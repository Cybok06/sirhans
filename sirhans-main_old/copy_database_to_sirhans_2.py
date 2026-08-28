from pymongo import MongoClient
from pymongo.server_api import ServerApi
from urllib.parse import quote_plus
from tqdm import tqdm

# =========================
# DATABASE 1 - SOURCE
# =========================
source_uri = "mongodb+srv://nagonu:0500868021Yaw@cluster0.yp3zg2d.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
source_client = MongoClient(source_uri, server_api=ServerApi("1"))
source_db = source_client["sirhans"]

# =========================
# DATABASE 2 - DESTINATION
# =========================
username = "zico_cybok"
password = "T7uF10RDgC5Im7Wp"

dest_uri = (
    f"mongodb+srv://{quote_plus(username)}:{quote_plus(password)}"
    "@cluster0.a77dwo1.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
)

dest_client = MongoClient(dest_uri, server_api=ServerApi("1"))
dest_db = dest_client["sirhans_2"]


def copy_database():
    print("Connecting to MongoDB...")

    source_client.admin.command("ping")
    dest_client.admin.command("ping")

    print("Connected successfully!")
    print("Starting database copy...\n")

    collections = source_db.list_collection_names()

    if not collections:
        print("No collections found in source database.")
        return

    for collection_name in collections:
        print(f"\nCopying collection: {collection_name}")

        source_collection = source_db[collection_name]
        dest_collection = dest_db[collection_name]

        total_docs = source_collection.count_documents({})

        if total_docs == 0:
            print(f"{collection_name} is empty. Skipping...")
            continue

        copied = 0
        batch = []

        cursor = source_collection.find({})

        for doc in tqdm(cursor, total=total_docs, desc=collection_name):
            batch.append(doc)

            if len(batch) >= 500:
                dest_collection.insert_many(batch, ordered=False)
                copied += len(batch)
                batch = []

        if batch:
            dest_collection.insert_many(batch, ordered=False)
            copied += len(batch)

        print(f"Done: {copied} documents copied to {collection_name}")

    print("\nFull database copy completed successfully!")


if __name__ == "__main__":
    copy_database()