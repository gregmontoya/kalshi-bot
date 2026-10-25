"""Firebase Firestore Live UI Sync Manager.

Syncs active baskets, open positions, pod statuses, and real-time execution alerts.
Includes graceful local fallback when Google Cloud / Firebase credentials are not provided.
"""

import json
import logging
from pathlib import Path
from typing import Any, Optional
from engine.config import settings

logger = logging.getLogger(__name__)


class FirebaseSyncManager:
    """Manages real-time state synchronization with Firebase Firestore."""

    def __init__(self, credentials_path: Optional[str] = None):
        self.credentials_path = credentials_path or settings.FIREBASE_CREDENTIALS_PATH
        self.collection_name = settings.FIREBASE_FIRESTORE_COLLECTION
        self._db = None
        self._local_fallback_cache: dict[str, Any] = {}
        self._init_firebase()

    def _init_firebase(self) -> None:
        """Initializes Firebase Admin SDK if credentials exist."""
        if not self.credentials_path or not Path(self.credentials_path).exists():
            logger.info("Firebase credentials not configured or file not found; running with local in-memory UI state sync.")
            return

        try:
            import firebase_admin
            from firebase_admin import credentials, firestore

            if not firebase_admin._apps:
                cred = credentials.Certificate(self.credentials_path)
                firebase_admin.initialize_app(cred)
            self._db = firestore.client()
            logger.info("Firebase Firestore connected successfully.")
        except Exception as exc:
            logger.warning("Could not initialize Firebase Firestore: %s; using local state sync.", exc)

    async def sync_state(self, document_id: str, data: dict[str, Any]) -> None:
        """Pushes state update to Firestore or local fallback cache."""
        self._local_fallback_cache[document_id] = data

        if self._db is not None:
            try:
                doc_ref = self._db.collection(self.collection_name).document(document_id)
                doc_ref.set(data, merge=True)
            except Exception as exc:
                logger.error("Error pushing state to Firestore for %s: %s", document_id, exc)

    def get_synced_state(self, document_id: str) -> Optional[dict[str, Any]]:
        """Retrieves synced state for given document ID."""
        if self._db is not None:
            try:
                doc = self._db.collection(self.collection_name).document(document_id).get()
                if doc.exists:
                    return doc.to_dict()
            except Exception as exc:
                logger.error("Error reading from Firestore: %s", exc)
        return self._local_fallback_cache.get(document_id)
