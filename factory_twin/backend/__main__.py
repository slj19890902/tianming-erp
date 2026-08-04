import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "factory_twin.backend.app:app",
        host="127.0.0.1",
        port=8092,
        reload=True,
    )
