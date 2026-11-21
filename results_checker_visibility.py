"""Persist visibility for the generated Results Checker service."""

RESULTS_CHECKER_SERVICE_ID = "results_checker_service"


def results_checker_display(database):
    settings = database["service_settings"].find_one({"_id": RESULTS_CHECKER_SERVICE_ID}) or {}
    return "OFF" if settings.get("display") == "OFF" else "ON"
