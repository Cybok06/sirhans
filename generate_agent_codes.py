from agent_code_utils import generate_missing_agent_codes


if __name__ == "__main__":
    result = generate_missing_agent_codes()
    print(
        "Agent code generation complete: "
        f"{result['created']} created, {result['skipped']} already had codes."
    )
