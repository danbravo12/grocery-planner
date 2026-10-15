"""Print the API's OpenAPI schema (the frontend's types are generated from it):

uv run python -m grocery_planner.api.openapi > ../frontend/openapi.json
"""

import json
import sys

from grocery_planner.api import ApiSettings, create_app

if __name__ == "__main__":
    app = create_app(ApiSettings(app_env="dev"))
    json.dump(app.openapi(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
