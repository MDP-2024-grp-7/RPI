import requests

API_IP = '192.168.7.27'
API_PORT = 5001

url = f"http://{API_IP}:{API_PORT}/path"
data = {
            "obstacles": [
                {
                    "x": 18,
                    "y": 18,
                    "d": 6,
                    "id": "1"
                }
            ],
            "retrying": False,
            "robot_x": 1,
            "robot_y": 1,
            "robot_dir": 0
        }
response = requests.post(url, json=data)
if response.status_code == 200:
    print("Success")
    response.json()
else:
    print("fail")

