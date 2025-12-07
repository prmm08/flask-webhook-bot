from flask import Flask, request

app = Flask(__name__)

@app.route("/signal", methods=["POST"])
def signal():
    data = request.json
    print("Signal empfangen:", data)
    return {"status": "ok"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
