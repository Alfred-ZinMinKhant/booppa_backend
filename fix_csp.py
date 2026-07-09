with open("app/services/csp_doc_generator.py", "r") as f:
    content = f.read()

# Replace OpenAI instantiation
old_client = """    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url=DEEPSEEK_BASE_URL)"""
new_client = """    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url=DEEPSEEK_BASE_URL, timeout=60.0)"""

if old_client in content:
    content = content.replace(old_client, new_client)
    with open("app/services/csp_doc_generator.py", "w") as f:
        f.write(content)
