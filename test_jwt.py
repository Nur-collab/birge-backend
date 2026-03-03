from auth import create_access_token, verify_token

token = create_access_token({"sub": 5})
print("Token:", token)
user_id = verify_token(token)
print("Decoded type:", type(user_id), "Value:", user_id)
