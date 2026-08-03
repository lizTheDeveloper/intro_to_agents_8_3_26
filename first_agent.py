
from util import prompt, prompt_ollama

done = False
## this is the state of the conversation, which will be passed to the prompt function
messages = []

## this is the agentic loop, which will continue until the user types "exit"
while not done:
    # this is the user prompt, which will be passed to the prompt function
    user_prompt = input("Enter your prompt: ")
    # here's how the user exits the loop
    if user_prompt.lower() == "exit":
        done = True
        break
    ## add the user's message to the overall conversation state, which will be passed to the prompt function
    messages.append({
        "role": "user",
        "content": user_prompt
    })
    ## send the whole conversation state to the prompt function, which will return the assistant's response
    response = prompt(messages)
    
    ## add the assistant's response to the overall conversation state, which will be passed to the prompt function
    messages.append({
        "role": "assistant",
        "content": response['choices'][0]['message']['content']
    })
    ## print the result to the console
    print(response['choices'][0]['message']['content'])