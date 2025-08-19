from flask import Flask, render_template, request, jsonify
import requests, os

app = Flask(__name__)

# ⚡ Yaha apna App ID & Secret dal de
APP_ID = "YOUR_APP_ID"
APP_SECRET = "YOUR_APP_SECRET"

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/get_page_tokens", methods=["POST"])
def get_page_tokens():
    try:
        data = request.get_json()
        user_token = data.get("user_token")

        if not user_token:
            return jsonify({"error": "❌ User Token missing"}), 400

        # ✅ Debug token API se validation
        debug_url = f"https://graph.facebook.com/debug_token?input_token={user_token}&access_token={APP_ID}|{APP_SECRET}"
        debug_res = requests.get(debug_url).json()

        if "error" in debug_res.get("data", {}):
            return jsonify({"error": "⚠️ Invalid User Token"}), 400

        # ✅ Get user pages
        url = f"https://graph.facebook.com/me/accounts?access_token={user_token}"
        res = requests.get(url).json()

        if "error" in res:
            return jsonify({"error": res['error']['message']}), 400

        page_tokens = []
        for page in res.get("data", []):
            page_tokens.append({
                "name": page.get("name"),
                "access_token": page.get("access_token")
            })

        if not page_tokens:
            return jsonify({"error": "⚠️ No pages found for this user"}), 400

        return jsonify({"page_tokens": page_tokens})

    except Exception as e:
        return jsonify({"error": f"❌ Server Error: {str(e)}"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
