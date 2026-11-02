# Live Posture Monitor — GitHub Codespaces

Phone: `https://YOUR-FORWARDED-URL/capture`
Laptop: `https://YOUR-FORWARDED-URL/dashboard`

1. Put `app.py` and `requirements.txt` in a GitHub repository and open it in Codespaces.
2. Run `pip install -r requirements.txt`
3. Run `uvicorn app:app --host 0.0.0.0 --port 8000`
4. In the Ports tab, forward port 8000 and keep it **Private**.
5. Open `/dashboard` on the laptop.
6. Open `/capture` on the phone, allow camera permission, and press Start streaming.

Use the actual HTTPS URL shown by Codespaces, not the placeholder. First inference can be slow while YOLO downloads weights. The prototype targets up to 12 processed frames/second and is intended for development, not clinical use. It supports one active camera/tracker at a time. Keep the forwarded port private.
