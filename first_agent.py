
from util import prompt, prompt_ollama, prompt_with_tools
from tools import fetch_youtube_transcript, get_arxiv_paper, tool_definitions
import json


done = False
just_called_tool = False
## this is the state of the conversation, which will be passed to the prompt function
messages = []

## this is the agentic loop, which will continue until the user types "exit"
while not done:
    # this is the user prompt, which will be passed to the prompt function
    if not just_called_tool:
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
    just_called_tool = False
    ## send the whole conversation state to the prompt function, which will return the assistant's response
    response = prompt_with_tools(messages, tool_definitions)
    
    ## if the agent has called a tool, we need to execute the tool and add the result to the conversation state
    if response['choices'][0]['finish_reason'] == 'tool_calls':
        tool_call = response['choices'][0]['message']['tool_calls'][0]
        tool_name = tool_call['function']['name']
        tool_call_id = tool_call['id']
        tool_args = json.loads(tool_call['function']['arguments'])
        messages.append({
            "role": "assistant",
            "tool_call_id": tool_call_id,
            "content": f"Calling tool {tool_name} with arguments {tool_args}"
        })
        
        
        if tool_name == 'fetch_youtube_transcript':
            tool_result = fetch_youtube_transcript(tool_args['video_id'])
        elif tool_name == 'get_arxiv_paper':
            tool_result = get_arxiv_paper(tool_args['doi'])
        else:
            tool_result = None
            
            
        ## add the tool's result to the conversation state
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": tool_result
        })
        just_called_tool = True
    
    print(response)
    ## if the agent called a tool they might not have said anything
    if response['choices'][0]['message']['content'] is not None:
        ## add the assistant's response to the overall conversation state, which will be passed to the prompt function
        messages.append({
            "role": "assistant",
            "content": response['choices'][0]['message']['content']
        })

## at the end of the loop, generate memories from the conversation
messages.append({
    "role": "system",
    "content": "Generate memories from this conversation. Summarize the conversation and extract any important information that should be remembered for future interactions. Details about the user, the environment, and short snippets of key takeaways from any videos you extract the summary of. Don't summarize the concept that we want a summary, only summarize the conversation and tool results."
})

memories_response = prompt(messages)
## write the memories to a file
with open("memories.txt", "a") as f:
    f.write(memories_response['choices'][0]['message']['content'] + "\n")