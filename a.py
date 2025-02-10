import os
import requests
import base64
import tempfile
from io import BytesIO
from PIL import Image
from moviepy import VideoFileClip
from flask import Flask, request, jsonify
import google.generativeai as genai
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)

# --- Configuration ---
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN")
INSTAGRAM_API_URL = (
    "https://graph.instagram.com/v21.0/me/messages"  # Corrected endpoint
)

# --- Gemini Setup ---
genai.configure(api_key=GOOGLE_API_KEY)

# --- Helper Functions ---


def video_to_frames(video_file, frame_interval=0.3):
    """
    Extracts frames from a video.

    Args:
        video_file: Path to the video file
        frame_interval: Interval between frames in seconds (default: 1)
    """
    try:
        # Validate frame interval
        frame_interval = float(frame_interval)

        video = VideoFileClip(video_file)
        frames = []
        duration = int(video.duration)

        # Extract frames at specified intervals
        for time in range(0, duration, frame_interval):
            frame = video.get_frame(time)
            img_bytes = BytesIO()
            Image.fromarray(frame).save(img_bytes, format="PNG")
            img_bytes.seek(0)
            frame_base64 = base64.b64encode(img_bytes.read()).decode("utf-8")
            frames.append(frame_base64)

        video.close()
        return frames
    except Exception as e:
        print(f"Error processing video: {e}")
        return []


def extract_audio(video_file):
    """Extracts audio to a temporary MP3."""
    try:
        video = VideoFileClip(video_file)
        audio_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3").name
        video.audio.write_audiofile(audio_file, codec="libmp3lame")
        video.close()
        return audio_file
    except Exception as e:
        print(f"Error extracting audio: {e}")
        return None


def summarize_video(video_file_path, prompt, frame_interval):
    """Summarizes video using Gemini."""
    model = genai.GenerativeModel("gemini-2.0-flash")
    frames = video_to_frames(video_file_path, frame_interval)
    audio_file = extract_audio(video_file_path)

    if not frames and not audio_file:
        print("No frames or audio extracted.")
        return "Could not summarize video: No frames or audio."

    contents = [prompt]
    for frame in frames:
        contents.append({"mime_type": "image/png", "data": frame})

    if audio_file:
        try:
            with open(audio_file, "rb") as f:
                audio_data = f.read()
            contents.append(
                {
                    "mime_type": "audio/mpeg",
                    "data": base64.b64encode(audio_data).decode("utf-8"),
                }
            )
        except Exception as e:
            print(f"Error reading audio: {e}")

    try:
        token_count = model.count_tokens(contents).total_tokens
        print(f"token count {token_count}")
        response = model.generate_content(contents)
        summary_text = response.text
        print(f"Generated summary: {summary_text}")
        print(f"Tokens used: {token_count}")
        return summary_text
    except Exception as e:
        print(f"Gemini API error: {e}")
        return f"Could not summarize video: Gemini API error: {e}"
    finally:
        if audio_file and os.path.exists(audio_file):
            os.remove(audio_file)


# --- Webhook Handlers ---


@app.route("/webhook", methods=["GET"])
def handle_verification():
    """Handles webhook verification."""
    if (
        request.args.get("hub.mode") == "subscribe"
        and request.args.get("hub.verify_token") == VERIFY_TOKEN
    ):
        print("Webhook verified!")
        return request.args.get("hub.challenge")
    else:
        return "Verification failed!", 403


@app.route("/webhook", methods=["POST"])
def handle_webhook():
    """Handles incoming webhook events."""
    data = request.get_json()
    print("Received webhook data:", data)

    try:
        if "object" in data and data["object"] == "instagram":
            for entry in data["entry"]:
                for messaging_event in entry["messaging"]:
                    if "message" in messaging_event:
                        sender_id = messaging_event["sender"]["id"]
                        if "attachments" in messaging_event["message"]:
                            for attachment in messaging_event["message"]["attachments"]:
                                if (
                                    attachment["type"] == "video"
                                    or attachment["type"] == "ig_reel"
                                ):
                                    video_url = attachment["payload"]["url"]
                                    print(
                                        f"Received video from {sender_id}: {video_url}"
                                    )
                                    # Send the "processing" message *immediately*
                                    send_reply(
                                        sender_id,
                                        "Processing your video, please wait...",
                                    )
                                    process_video_message(sender_id, video_url)
                        elif "text" in messaging_event["message"]:
                            message_text = messaging_event["message"]["text"]
                            print(f"Received message from {sender_id}: {message_text}")
                            send_reply(sender_id, "Thanks for your message!")

    except KeyError as e:
        print(f"KeyError: {e} - Check webhook payload structure.")
        return jsonify({"status": "error", "message": f"KeyError: {e}"}), 400
    except Exception as e:
        print(f"An error occurred: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify({"status": "success"}), 200


# --- Message Processing and Sending ---


def download_video(video_url):
    """Downloads video and saves to a temporary file."""
    try:
        response = requests.get(video_url, stream=True)
        response.raise_for_status()

        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        with open(temp_file.name, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return temp_file.name

    except requests.exceptions.RequestException as e:
        print(f"Error downloading video: {e}")
        return None


def send_reply(sender_id, message_text):
    """Sends a reply message."""
    params = {"access_token": PAGE_ACCESS_TOKEN}
    headers = {"Content-Type": "application/json"}
    data = {
        "recipient": {"id": sender_id},
        "message": {"text": message_text},
    }
    response = requests.post(
        INSTAGRAM_API_URL, params=params, headers=headers, json=data
    )
    if response.status_code != 200:
        print(f"Error sending reply: {response.status_code} - {response.text}")


def process_video_message(sender_id, video_url):
    """Downloads, summarizes, and replies."""
    video_file_path = download_video(video_url)
    if not video_file_path:
        send_reply(sender_id, "Sorry, I couldn't download the video.")
        return

    try:
        prompt = "Summarize this video under 100 words"
        summary = summarize_video(video_file_path, prompt=prompt, frame_interval=2)
        # Send the *final* summary
        send_reply(sender_id, summary)
    finally:
        if video_file_path and os.path.exists(video_file_path):
            os.remove(video_file_path)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
