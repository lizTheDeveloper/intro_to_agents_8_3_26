import requests
import json
import os

from dotenv import load_dotenv
load_dotenv()

def prompt(messages):
    response = requests.post(
    url="https://openrouter.ai/api/v1/chat/completions",
    headers={
        "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}"
    },
    data=json.dumps({
        "model": "openrouter/free",
        "messages": messages
    })
    )
    
    return response.json()

def prompt_with_tools(messages, tools=[]):
    response = requests.post(
    url="https://openrouter.ai/api/v1/chat/completions",
    headers={
        "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}"
    },
    data=json.dumps({
            "model": "openrouter/auto-beta",
            "messages": messages,
            "tools": tools
        })
    )
    
    return response.json()

def prompt_ollama(messages):
    from ollama import Client
    client = Client(
        host='http://localhost:11434',
    )
    response = client.chat(model='llama3.2:3b', messages=messages)
    return response.json()