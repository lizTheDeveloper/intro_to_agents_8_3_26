import os
import subprocess
import requests

def fetch_youtube_transcript(video_id):
    ## use yt-dlp to fetch the transcript
    command = ['yt-dlp', '--write-auto-sub', '--sub-lang', 'en', '--skip-download', '-o', f'{video_id}', f'https://www.youtube.com/watch?v={video_id}']
    subprocess.run(command)
    ## read the transcript file
    transcript_file = f'{video_id}.en.vtt'
    if os.path.exists(transcript_file):
        with open(transcript_file, 'r') as f:
            transcript = f.read()
        
        return transcript[0:3000]  ## return the first 3000 characters of the transcript
    return None


def get_arxiv_paper(doi):
    url = f"https://arxiv.org/abs/{doi}"
    response = requests.get(url)
    if response.status_code == 200:
        return response.text
    else:
        return None

    
tool_definitions = [
    {
        "name": "fetch_youtube_transcript",
        "description": "Fetches the transcript of a YouTube video given its video ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "video_id": {
                    "type": "string",
                    "description": "The ID of the YouTube video."
                }
            },
            "required": ["video_id"]
        }
    },
    {
        "name": "get_arxiv_paper",
        "description": "Fetches the content of an arXiv paper given its DOI.",
        "parameters": {
            "type": "object",
            "properties": {
                "doi": {
                    "type": "string",
                    "description": "The DOI of the arXiv paper."
                }
            },
            "required": ["doi"]
        }
    }
]


if __name__ == "__main__":
    video_id = "F3lL98Pj90o"  # Replace with the actual YouTube video ID
    transcript = fetch_youtube_transcript(video_id)
    if transcript:
        print(transcript)
    else:
        print("Transcript not found.")
        
        
        
### F3lL98Pj90o this YouTube video transcript with your YouTube transcript tool.