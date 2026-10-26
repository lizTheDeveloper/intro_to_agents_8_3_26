from util import prompt
import json

done = False
messages = []

while not done:
    user_prompt = input("Enter your prompt: ")
    
    if user_prompt.lower() == "exit":
        done = True
        break
    messages.append({
        "role": "user",
        "content": user_prompt
    })
    response = prompt(messages)
    
    print(response)
    
    
