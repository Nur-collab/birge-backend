import os

# Тестовая БД должна быть выбрана до импорта main/database.
os.environ["DATABASE_URL"] = "sqlite:///./_test_birge.db"
os.environ["RENDER_EXTERNAL_URL"] = ""
os.environ["ENVIRONMENT"] = "test"

import asyncio
import unittest
from datetime import datetime, timedelta

from fastapi import HTTPException

import database
import models
import schemas
import main


class FlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.Base.metadata.drop_all(bind=database.engine)
        database.Base.metadata.create_all(bind=database.engine)

    def setUp(self):
        self.db = database.SessionLocal()
        self.db.query(models.ChatMessage).delete()
        self.db.query(models.TripRequest).delete()
        self.db.query(models.Review).delete()
        self.db.query(models.Trip).delete()
        self.db.query(models.User).delete()
        self.db.commit()

        self.driver = models.User(
            name="Driver",
            phone="+996555000001",
            mbank_phone="+996555000001",
            elkart_number="123456",
        )
        self.passenger = models.User(name="Passenger", phone="+996555000002")
        self.outsider = models.User(name="Outsider", phone="+996555000003")
        self.db.add_all([self.driver, self.passenger, self.outsider])
        self.db.commit()
        for user in (self.driver, self.passenger, self.outsider):
            self.db.refresh(user)

        self.today = main.get_bishkek_today_str()
        self.tomorrow = (
            datetime.strptime(self.today, "%Y-%m-%d") + timedelta(days=1)
        ).strftime("%Y-%m-%d")

    def tearDown(self):
        self.db.close()

    def _trip(self, user, *, role="driver", trip_date=None, status="active", seats=3, seats_taken=0):
        trip = models.Trip(
            user_id=user.id,
            role=role,
            origin="Ала-Арча",
            destination="ЦУМ",
            time="08:15",
            date=trip_date,
            status=status,
            seats=seats,
            seats_taken=seats_taken,
        )
        self.db.add(trip)
        self.db.commit()
        self.db.refresh(trip)
        return trip

    def _request(self, trip, requester, *, requester_trip=None, status="pending"):
        req = models.TripRequest(
            trip_id=trip.id,
            requester_trip_id=requester_trip.id if requester_trip else None,
            requester_id=requester.id,
            driver_id=trip.user_id,
            status=status,
        )
        self.db.add(req)
        self.db.commit()
        self.db.refresh(req)
        return req

    def test_passenger_cannot_set_matched(self):
        trip = self._trip(self.driver, trip_date=self.today)
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(main.update_trip_status(
                trip.id,
                schemas.TripStatusUpdate(status="matched"),
                current_user=self.passenger,
                db=self.db,
            ))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_future_scheduled_trip_stays_scheduled_when_full(self):
        trip = self._trip(self.driver, trip_date=self.tomorrow, status="scheduled", seats=1)
        passenger_trip = self._trip(self.passenger, role="passenger", trip_date=self.tomorrow, status="scheduled")
        req = self._request(trip, self.passenger, requester_trip=passenger_trip)
        other_req = self._request(trip, self.outsider, status="pending")

        asyncio.run(main.respond_to_request(
            req.id,
            schemas.TripRequestStatusUpdate(status="accepted"),
            current_user=self.driver,
            db=self.db,
        ))

        self.db.refresh(trip)
        self.db.refresh(passenger_trip)
        self.db.refresh(other_req)
        self.assertEqual(trip.status, "scheduled")
        self.assertEqual(passenger_trip.status, "scheduled")
        self.assertEqual(other_req.status, "declined")

        scheduled = main.read_scheduled_trips(current_user=self.driver, db=self.db)
        self.assertTrue(any(item["trip_id"] == trip.id for item in scheduled))

        matches = main.find_matches(
            role="passenger",
            origin="Ала-Арча",
            destination="ЦУМ",
            time="08:15",
            date=self.tomorrow,
            current_user=self.outsider,
            db=self.db,
        )
        self.assertFalse(any(m.id == trip.id for m in matches))

    def test_review_requires_completed_trip(self):
        trip = self._trip(self.driver, trip_date=self.tomorrow, status="scheduled")
        passenger_trip = self._trip(self.passenger, role="passenger", trip_date=self.tomorrow, status="scheduled")
        req = self._request(trip, self.passenger, requester_trip=passenger_trip, status="accepted")

        with self.assertRaises(HTTPException) as ctx:
            main.create_review(
                schemas.ReviewCreate(user_id=self.driver.id, rating=5, text="early", trip_id=trip.id),
                current_user=self.passenger,
                db=self.db,
            )
        self.assertEqual(ctx.exception.status_code, 403)

        req.status = "completed"
        trip.status = "completed"
        trip.date = self.today
        self.db.commit()

        review = main.create_review(
            schemas.ReviewCreate(user_id=self.driver.id, rating=5, text="later", trip_id=trip.id),
            current_user=self.passenger,
            db=self.db,
        )
        self.assertEqual(review.rating, 5)

    def test_foreign_requester_trip_id_rejected(self):
        driver_trip = self._trip(self.driver, trip_date=self.today)
        foreign_trip = self._trip(self.outsider, role="passenger", trip_date=self.today)

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(main.send_trip_request(
                schemas.TripRequestCreate(trip_id=driver_trip.id, requester_trip_id=foreign_trip.id),
                current_user=self.passenger,
                db=self.db,
            ))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_owner_schema_keeps_payment_fields(self):
        payload = schemas.User.model_validate(self.driver).model_dump()
        self.assertEqual(payload["mbank_phone"], self.driver.mbank_phone)
        self.assertEqual(payload["reviews"], [])

    def test_public_trip_payload_has_no_payment_fields(self):
        trip = self._trip(self.driver, trip_date=self.today)
        payload = schemas.Trip.model_validate(trip).model_dump()
        self.assertIsNotNone(payload.get("user"))
        self.assertNotIn("mbank_phone", payload["user"])
        self.assertNotIn("elkart_number", payload["user"])

    def test_payment_only_for_participants(self):
        trip = self._trip(self.driver, trip_date=self.today)
        self._request(trip, self.passenger, status="accepted")

        payment = main.get_trip_payment(trip.id, current_user=self.passenger, db=self.db)
        self.assertEqual(payment["mbank_phone"], self.driver.mbank_phone)

        with self.assertRaises(HTTPException) as ctx:
            main.get_trip_payment(trip.id, current_user=self.outsider, db=self.db)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_driver_cancel_syncs_requests_and_passenger_trip(self):
        trip = self._trip(self.driver, trip_date=self.today, status="matched", seats=1, seats_taken=1)
        passenger_trip = self._trip(self.passenger, role="passenger", trip_date=self.today, status="matched")
        req = self._request(trip, self.passenger, requester_trip=passenger_trip, status="accepted")

        asyncio.run(main.update_trip_status(
            trip.id,
            schemas.TripStatusUpdate(status="cancelled"),
            current_user=self.driver,
            db=self.db,
        ))

        self.db.refresh(trip)
        self.db.refresh(req)
        self.db.refresh(passenger_trip)
        self.assertEqual(trip.status, "cancelled")
        self.assertEqual(trip.seats_taken, 0)
        self.assertEqual(req.status, "cancelled")
        self.assertEqual(passenger_trip.status, "active")

    def test_chat_requires_participant_and_saves_message(self):
        trip = self._trip(self.driver, trip_date=self.today)
        self._request(trip, self.passenger, status="accepted")

        message = asyncio.run(main.send_trip_message(
            trip.id,
            schemas.ChatMessageInput(text="Привет"),
            current_user=self.passenger,
            db=self.db,
        ))
        self.assertEqual(message.text, "Привет")
        self.assertEqual(self.db.query(models.ChatMessage).count(), 1)

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(main.send_trip_message(
                trip.id,
                schemas.ChatMessageInput(text="Чужой"),
                current_user=self.outsider,
                db=self.db,
            ))
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
