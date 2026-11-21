"""DataKazina routes selected independently of the local MTN service name."""
PROVIDERS = {"datakazina_mtn": 3, "datakazina_mtn_express": 6}
ALL_PROVIDERS = {"datakazina", *PROVIDERS}


def network_id(provider, service_name):
    if provider in PROVIDERS:
        return PROVIDERS[provider]
    # Preserve existing services using the original provider setting.
    return 6 if str(service_name or "").strip().lower() == "mtn express" else 3


def supports_service(service):
    return str(service.get("name") or "").strip().lower() in {"mtn normal", "mtn express"}
