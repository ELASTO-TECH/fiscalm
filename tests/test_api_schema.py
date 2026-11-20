import json
from pathlib import Path

from test_api import (
    _req,  # noqa: F401
    server as server,
    world as world,
)

from fiscalm import PRODUCT, __version__
from fiscalm.api import actions, auth, lists, schema as api_schema, state as api, v1 as api_v1


def test_openapi_copy_and_route_bijection():
    spec = api_schema.openapi()
    assert spec == json.loads((Path(__file__).resolve().parents[1] / "docs/openapi.json").read_text())
    assert spec["x-app-action-effects"] == json.loads(
        (Path(__file__).resolve().parents[1] / "app/src/lib/action-effects.json").read_text()
    )
    assert set(spec["x-app-action-roles"]) == set(actions.ACTIONS)
    assert spec["x-app-action-roles"] == json.loads(
        (Path(__file__).resolve().parents[1] / "app/src/lib/action-roles.json").read_text()
    )
    assert set(spec["x-app-action-roles"].values()) <= auth.capabilities(None).keys()
    assert spec["openapi"] == "3.1.0"
    action = spec["paths"]["/api/v1/companies/{co}/actions/{action}"]["post"]
    assert set(action["x-action-fields"]) == set(actions.API_ACTIONS)
    assert {(r.method.lower(), "/api/v1" + r.path) for r in api_v1.ROUTES} == {
        (method, path) for path, methods in spec["paths"].items() for method in methods
    }
    for route in api_v1.ROUTES:
        assert route.schema in spec["components"]["schemas"]
    assert spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    paths = spec["paths"]
    assert set(paths["/api/v1/companies/{co}/documents/{sha}/file"]["get"]["responses"]["200"]["content"]) == set(
        api.MEDIA.values()
    )
    revoke = paths["/api/v1/companies/{co}/upload-links/{id}/revoke"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert set(revoke["properties"]) == {"co"} and revoke["additionalProperties"] is False


def test_route_table_supports_shared_path_methods(monkeypatch):
    post = api_v1.Route("POST", "/companies", "test_create_company", "Company", "write")
    monkeypatch.setattr(api_v1, "ROUTES", (*api_v1.ROUTES, post))
    monkeypatch.setattr(api_schema, "ROUTES", api_v1.ROUTES)
    assert api_v1.match("/api/v1/companies", "POST")[0] == post
    assert api_v1.match("/api/v1/companies", "GET")[0].resource == "companies"
    assert set(api_schema.openapi()["paths"]["/api/v1/companies"]) == {"get", "post"}


def test_openapi_is_public_but_not_data(server):
    code, headers, raw = _req(server, "GET", "/api/v1/openapi.json")
    assert code == 200 and headers["fiscalm-api-version"] == "1"
    spec = json.loads(raw)
    assert spec == api_schema.openapi()
    assert spec["paths"]["/api/v1/openapi.json"]["get"]["security"] == []
    assert _req(server, "GET", "/api/v1/companies")[0] == 401


def test_state_schema_covers_response_fields(ledger, world):
    definitions = api_schema.schemas()
    schema = definitions["State"]
    response = api_v1.public_state(api.state(ledger, "NORD"), ledger)
    assert set(schema["properties"]) == set(schema["required"]) == set(response)
    expense = schema["properties"]["expenses"]
    assert expense["type"] == "array"
    assert expense["items"] == api_schema.obj(
        {
            "id": {"type": "string"},
            "company": {"type": "string"},
            "invoice": {"type": ["string", "null"]},
            "document": {"type": ["string", "null"]},
            "sources": {"type": "array", "items": {"enum": ["efactura", "mail", "upload_link", "upload", "history"]}},
        }
    )
    partner = schema["properties"]["partners"]["items"]
    assert set(partner["required"]) == set(response["partners"][0])
    status = partner["properties"]["anaf_status"]["anyOf"][0]
    for flag in ("vat_payer", "efactura_registered", "inactive", "vat_on_collection"):
        assert status["properties"][flag] == {"type": ["boolean", "null"]}

    file = schema["properties"]["files"]["items"]
    assert response["files"]
    assert all(set(row) == set(file["required"]) and "payments" not in row for row in response["files"])


def test_summary_contract_keeps_shell_fields_and_complete_report_counts(ledger, world):
    response = api_v1.public_state(lists.summary(ledger, "NORD"), ledger)
    properties = api_schema.schemas()["StateSummary"]["properties"]
    assert set(response) == set(properties)
    full = api.state(ledger, "NORD")
    for old, small in zip(full["reports"], response["reports"], strict=True):
        assert small["counts"]["to_check"] == len(old["to_check"])
        assert small["counts"]["card"] == (len(old["bank"]["card"]) if old["bank"] else 0)
        assert {k: v for k, v in old.items() if not isinstance(v, (list, dict))} == {
            k: v for k, v in small.items() if not isinstance(v, (list, dict))
        }
    assert response["files"] == full["files"]


def test_public_product_metadata_uses_the_installed_version(server):
    code, _, raw = _req(server, "GET", "/api/v1/meta")
    assert code == 200
    assert json.loads(raw) == {"product": PRODUCT, "version": __version__}
