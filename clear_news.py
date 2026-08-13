import os
import json
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

load_dotenv()

# Initialize Firebase Admin
service_account_info = json.loads(
    os.environ.get("FIREBASE_SERVICE_ACCOUNT")
)

cred = credentials.Certificate(service_account_info)

firebase_admin.initialize_app(cred)

db = firestore.client()

# Collection cần clear
collection_ref = db.collection("semiconductor_news")


def clear_collection(collection_ref, batch_size=500):
    while True:
        docs = list(collection_ref.limit(batch_size).stream())

        if not docs:
            break

        batch = db.batch()

        for doc in docs:
            batch.delete(doc.reference)

        batch.commit()

        print(f"Deleted {len(docs)} documents")


if __name__ == "__main__":
    clear_collection(collection_ref)
    print("Collection 'semiconductor_news' has been cleared.")