import re

with open('main.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Закомментировать/удалить `/users/` GET и POST
content = re.sub(
    r'@app\.get\("/users/", response_model=List\[schemas\.User\]\)\ndef read_users\(db: Session = Depends\(get_db\)\):.*?(?=@app\.post\("/users/")',
    '', content, flags=re.DOTALL
)

content = re.sub(
    r'@app\.post\("/users/", response_model=schemas\.User\)\ndef create_user\(user: schemas\.UserCreate, db: Session = Depends\(get_db\)\):.*?(?=# --- AUTH ---)',
    '', content, flags=re.DOTALL
)

# 2. POST /trips/
content = re.sub(
    r'def create_trip\(trip: schemas\.TripCreate, db: Session = Depends\(get_db\)\):',
    r'def create_trip(trip: schemas.TripCreate, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):',
    content
)
# Inside create_trip:
# user = db.query(models.User).filter(models.User.id == trip.user_id).first()
# change to trip.user_id to current_user.id
content = re.sub(
    r'user = db\.query\(models\.User\)\.filter\(models\.User\.id == trip\.user_id\)\.first\(\)',
    r'user = current_user',
    content
)
content = re.sub(
    r'trip_data = trip\.model_dump\(\)',
    r'trip_data = trip.model_dump()\n    trip_data["user_id"] = user.id',
    content
)

# 3. /trips/matches
content = re.sub(
    r'def find_matches\(user_id: int, .*?, db: Session = Depends\(get_db\)\):',
    r'def find_matches(role: str, origin: str, destination: str, time: str, date: Optional[str] = None, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):',
    content
)
content = content.replace("models.Trip.user_id != user_id", "models.Trip.user_id != current_user.id")

# 4. /trips/{trip_id}/passengers
content = re.sub(
    r'def get_trip_passengers\(trip_id: int, db: Session = Depends\(get_db\)\):',
    r'def get_trip_passengers(trip_id: int, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):',
    content
)

# 5. /trips/{trip_id}/messages
content = re.sub(
    r'def get_trip_messages\(trip_id: int, db: Session = Depends\(get_db\)\):',
    r'def get_trip_messages(trip_id: int, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):',
    content
)

# 6. /trips/{trip_id}/status PATCH
content = re.sub(
    r'def update_trip_status\(trip_id: int, payload: dict, db: Session = Depends\(get_db\)\):',
    r'def update_trip_status(trip_id: int, payload: dict, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):',
    content
)

# 7. POST /reviews/
content = re.sub(
    r'def create_review\(review: schemas\.ReviewCreate, db: Session = Depends\(get_db\)\):',
    r'def create_review(review: schemas.ReviewCreate, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):',
    content
)
# author_name=review.author_name -> author_name=current_user.name
content = re.sub(
    r'author_name=review\.author_name,',
    r'author_name=current_user.name,',
    content
)

# 8. /trip-requests/ POST
content = re.sub(
    r'def create_trip_request\(payload: dict, db: Session = Depends\(get_db\)\):',
    r'def create_trip_request(payload: dict, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):',
    content
)
content = re.sub(
    r'requester_id = payload\.get\("requester_id"\)',
    r'requester_id = current_user.id',
    content
)

# 9. GET /trip-requests/incoming/{user_id}
content = re.sub(
    r'@app\.get\("/trip-requests/incoming/\{user_id\}"\)',
    r'@app.get("/trip-requests/incoming/")',
    content
)
content = re.sub(
    r'def get_incoming_requests\(user_id: int, db: Session = Depends\(get_db\)\):',
    r'def get_incoming_requests(current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):',
    content
)
content = re.sub(
    r"driver_trips = db\.query\(models\.Trip\)\.filter\(models\.Trip\.user_id == user_id, .*?\)\.all\(\)",
    r'driver_trips = db.query(models.Trip).filter(models.Trip.user_id == current_user.id, models.Trip.role == "driver").all()',
    content
)

# 10. PATCH /trip-requests/{request_id}
content = re.sub(
    r'def update_trip_request\(request_id: int, payload: dict, db: Session = Depends\(get_db\)\):',
    r'def update_trip_request(request_id: int, payload: dict, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):',
    content
)

# 11. GET /trip-requests/status/{requester_id}/{trip_id}
content = re.sub(
    r'@app\.get\("/trip-requests/status/\{requester_id\}/\{trip_id\}"\)',
    r'@app.get("/trip-requests/status/{trip_id}")',
    content
)
content = re.sub(
    r'def get_request_status\(requester_id: int, trip_id: int, db: Session = Depends\(get_db\)\):',
    r'def get_request_status(trip_id: int, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):',
    content
)
content = content.replace("models.TripRequest.requester_id == requester_id,", "models.TripRequest.requester_id == current_user.id,")

with open('patch_main.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Patch generated.")
