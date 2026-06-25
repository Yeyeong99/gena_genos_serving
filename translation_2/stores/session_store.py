"""
Session store module.

This module implements the `SessionStore` class, which handles the persistence of session data
using Redis or in-memory storage depending on the environment.
"""
import os
import json
import datetime
from abc import ABC, abstractmethod
from typing import List, Optional, Any, Dict, Union

import redis.asyncio as redis
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    TimeoutError as RedisTimeoutError,
    RedisError
)
from utils.logger import get_logger

log = get_logger(__name__)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")



class SessionBackend(ABC):
    """
    Abstract base class for session storage backends.
    """
    
    @abstractmethod
    async def get(self, session_id: str, key: Union[str, List[str]]) -> Optional[Union[Dict, List[Dict]]]:
        """
        Retrieve data from the store.
        """

    @abstractmethod
    async def save(self, session_id: str, ttl_seconds: int, **objects: Dict) -> None:
        """
        Save data to the store.
        """


class MemoryBackend(SessionBackend):
    """
    In-memory storage backend using a dictionary.
    """
    _storage: Dict[str, Dict] = {}

    async def get(self, session_id: str, key: Union[str, List[str]]) -> Optional[Union[Dict, List[Dict]]]:
        session_data = self._storage.get(session_id)
        if not session_data:
            return None
        if isinstance(key, str):
            return session_data.get(key)
        if isinstance(key, list):
            return [session_data.get(k) for k in key]
        return None

    async def save(self, session_id: str, ttl_seconds: int, **objects: Dict) -> None:
        if session_id not in self._storage:
            self._storage[session_id] = {}
        
        updated_at = datetime.datetime.now(datetime.UTC).isoformat() + "Z"
        self._storage[session_id].update(objects)
        self._storage[session_id]["updatedAt"] = updated_at


class RedisBackend(SessionBackend):
    """
    Redis storage backend.
    """
    def __init__(self, redis_url: str):
        self.redis_client = redis.from_url(redis_url, decode_responses=True)

    async def get(self, session_id: str, key: Union[str, List[str]]) -> Optional[Union[Dict, List[Dict]]]:
        try:
            raw = await self.redis_client.get(f"session:{session_id}")
            if not raw:
                return None
            
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as e:
                log.error("JSON Decode error for session %s: %s", session_id, e)
                return None

            if isinstance(key, str):
                return payload.get(key)
            if isinstance(key, list):
                return [payload.get(k) for k in key]
            
            log.error("Invalid key type for session %s: %s", session_id, type(key))
            return None

        except (RedisConnectionError, RedisTimeoutError) as e:
            log.error("Redis connection/timeout error for session %s: %s", session_id, e)
            return None
        except RedisError as e:
            log.error("Redis generic error for session %s: %s", session_id, e)
            return None

    async def save(self, session_id: str, ttl_seconds: int, **objects: Dict) -> None:
        try:
            # Optimistic locking or simple get-set is usually enough for session store if not highly concurrent on same session
            # For simplicity and existing pattern, we do get-update-set
            raw = await self.redis_client.get(f"session:{session_id}")
            updated_at = datetime.datetime.now(datetime.UTC).isoformat() + "Z"
            
            if raw:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = {}
                payload.update(objects)
                payload["updatedAt"] = updated_at
            else:
                payload = {
                    **objects,
                    "updatedAt": updated_at
                }

            data = json.dumps(payload, ensure_ascii=False)
            if ttl_seconds:
                await self.redis_client.setex(f"session:{session_id}", ttl_seconds, data)
            else:
                await self.redis_client.set(f"session:{session_id}", data)

        except (RedisConnectionError, RedisTimeoutError) as e:
            log.error("Redis save failed (connection/timeout) for session %s: %s", session_id, e)
        except (TypeError, ValueError, RedisError) as e:
            log.error("Redis save failed (generic) for session %s: %s", session_id, e)


class SessionStore:
    """
    Class for managing session data persistence.
    Delegates to RedisBackend or MemoryBackend.
    """
    _backend: Optional[SessionBackend] = None

    def __init__(self, session_id: str) -> None:
        """
        Initialize the session store.

        Args:
            session_id (str): The ID of the session.
        """
        self.session_id = session_id
        if self.session_id is None:
            log.warning("Session set to None")
        
        self.backend = self._get_backend()

    @classmethod
    def _get_backend(cls) -> SessionBackend:
        """
        Factory method to get or create the appropriate backend.
        """
        if cls._backend:
            return cls._backend
            
        # Determine backend
        profiles = os.getenv("COMPOSE_PROFILES", "").split(",")
        use_redis = "develop" in profiles
        
        if use_redis:
            try:
                log.info("Initializing RedisBackend with URL: %s", REDIS_URL)
                cls._backend = RedisBackend(REDIS_URL)
            except Exception as e:
                log.error("Failed to initialize RedisBackend, falling back to MemoryBackend: %s", e)
                cls._backend = MemoryBackend()
        else:
            log.info("Initializing MemoryBackend")
            cls._backend = MemoryBackend()
            
        return cls._backend

    async def get_objects(self, key: Union[str, List[str]] = "messages") -> Optional[Union[Dict, List[Dict]]]:
        """
        Retrieve objects from the session store by key.

        Args:
            key (str or List[str], optional): The key(s) to retrieve. Defaults to "messages".

        Returns:
            Optional[Dict | List[Dict]]: The retrieved object(s) or None if not found.
        """
        if not self.session_id:
            return None
        return await self.backend.get(self.session_id, key)

    async def save_objects(self, ttl_seconds: int = 7 * 24 * 3600, **objects: Any) -> None:
        """
        Save objects to the session store.

        Args:
            ttl_seconds (int, optional): Time-to-live in seconds. Defaults to 7 days.
            **objects: Key-value pairs of objects to save.
        """
        if not self.session_id:
            log.warning("Attempted to save objects with no session_id")
            return
        await self.backend.save(self.session_id, ttl_seconds, **objects)
