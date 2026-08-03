import os
import subprocess
import requests

def fetch_youtube_transcript(video_id):
    ## use yt-dlp to fetch the transcript
    command = ['yt-dlp', '--write-auto-sub', '--sub-lang', 'en', '--skip-download', f'https://www.youtube.com/watch?v={video_id}']
    subprocess.run(command)
    ## read the transcript file
    transcript_file = f'{video_id}.en.vtt'
    if os.path.exists(transcript_file):
        with open(transcript_file, 'r') as f:
            transcript = f.read()
        return transcript
    return None


if __name__ == "__main__":
    video_id = "F3lL98Pj90o"  # Replace with the actual YouTube video ID
    transcript = fetch_youtube_transcript(video_id)
    if transcript:
        print(transcript)
    else:
        print("Transcript not found.")