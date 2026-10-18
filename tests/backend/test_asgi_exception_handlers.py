import pytest
from app.core.asgi import EphemresApp, TestClient, HTTPException, JSONResponse, Request


class CustomForbiddenException(HTTPException):
    def __init__(self, detail: str = "Access Denied Custom"):
        super().__init__(status_code=403, detail=detail)


def test_custom_exception_handlers_404_subclass_and_valueerror():
    app = EphemresApp()

    invoked = {}

    @app.exception_handler(404)
    async def not_found_handler(request: Request, exc: HTTPException):
        invoked["404"] = True
        return JSONResponse({"custom": "not found here"}, status_code=404)

    @app.exception_handler(CustomForbiddenException)
    async def custom_forbidden_handler(request: Request, exc: CustomForbiddenException):
        invoked["subclass"] = True
        return JSONResponse({"custom_forbidden": exc.detail}, status_code=403)

    @app.exception_handler(ValueError)
    def value_error_handler(request: Request, exc: ValueError):
        invoked["value_error"] = True
        return JSONResponse({"error": "handled_value_error", "msg": str(exc)}, status_code=400)

    @app.get("/trigger-forbidden")
    async def trigger_forbidden():
        raise CustomForbiddenException("Specific forbidden message")

    @app.get("/trigger-value-error")
    async def trigger_val_err():
        raise ValueError("custom bad value")

    @app.get("/trigger-explicit-404")
    async def trigger_explicit_404():
        raise HTTPException(status_code=404, detail="Explicit 404")

    client = TestClient(app)

    # 1. 404 handler invoked on unmatched route
    r_unmatched = client.get("/non-existent-route")
    assert r_unmatched.status_code == 404
    assert r_unmatched.json() == {"custom": "not found here"}
    assert invoked.get("404") is True

    # 2. 404 handler invoked on route explicitly raising HTTPException(404)
    invoked["404"] = False
    r_explicit_404 = client.get("/trigger-explicit-404")
    assert r_explicit_404.status_code == 404
    assert r_explicit_404.json() == {"custom": "not found here"}
    assert invoked.get("404") is True

    # 3. HTTPException subclass handler invoked
    r_forbidden = client.get("/trigger-forbidden")
    assert r_forbidden.status_code == 403
    assert r_forbidden.json() == {"custom_forbidden": "Specific forbidden message"}
    assert invoked.get("subclass") is True

    # 4. ValueError handler invoked
    r_val = client.get("/trigger-value-error")
    assert r_val.status_code == 400
    assert r_val.json() == {"error": "handled_value_error", "msg": "custom bad value"}
    assert invoked.get("value_error") is True
