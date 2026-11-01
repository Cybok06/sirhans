from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi

# MongoDB Atlas URI
uri = "mongodb+srv://viresender_db_user:0500868021%40Yaw@projects.jarvdho.mongodb.net/?appName=Projects"

# Create client with stable API version
client = MongoClient(uri, server_api=ServerApi("1"))

# Try to connect and ping the cluster
try:
    client.admin.command("ping")
    print("Successfully connected to MongoDB!")
except Exception as e:
    print("MongoDB connection error:", e)

# Access your database
db = client["sirhans1"]
