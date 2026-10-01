"""A minimal file receiver used to prove multipart encoding over HTTP.

Lives in its own module so its route annotations resolve normally; a route
defined inside a module that uses ``from __future__ import annotations`` would
leave ``UploadFile`` as an unresolvable forward reference.
"""

from __future__ import annotations

from fastapi import FastAPI, File, UploadFile


def build_receiver() -> FastAPI:
    receiver = FastAPI()

    @receiver.post("/upload")
    async def receive(file: UploadFile = File(...)) -> dict:
        return {
            "filename": file.filename,
            "content_type": file.content_type,
            "content": (await file.read()).decode(),
        }

    return receiver
