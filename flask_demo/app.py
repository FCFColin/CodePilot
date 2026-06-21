"""Flask Web 应用示例 - 包含首页和 API 端点。"""

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)


@app.route("/")
def index():
    """首页：渲染欢迎页面模板。"""
    return render_template("index.html")


@app.route("/api/hello")
def api_hello():
    """API 端点：返回 JSON 问候信息。"""
    name = request.args.get("name", "World")
    return jsonify({"message": f"Hello, {name}!", "status": "ok"})


@app.route("/api/time")
def api_time():
    """API 端点：返回当前时间戳。"""
    from datetime import datetime

    now = datetime.now()
    return jsonify({
        "timestamp": now.isoformat(),
        "unix": int(now.timestamp()),
        "status": "ok",
    })


@app.route("/api/echo", methods=["POST"])
def api_echo():
    """API 端点：回显 POST 请求的 JSON 数据。"""
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "请发送有效的 JSON 数据", "status": "error"}), 400
    return jsonify({"echo": data, "status": "ok"})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
