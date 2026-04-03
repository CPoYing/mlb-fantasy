import os
import smtplib
import json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()

TOKEN_FILE = "tokens.json"

def save_tokens(access_token, refresh_token):
    """Persist tokens to file for scheduled jobs."""
    with open(TOKEN_FILE, "w") as f:
        json.dump({"access_token": access_token, "refresh_token": refresh_token}, f)

def load_tokens():
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE) as f:
        return json.load(f)

def build_report_html(leagues_data):
    rows = ""
    for league in leagues_data:
        rows += f"""
        <tr>
            <td style="padding:8px;border:1px solid #ddd">{league.get('name','')}</td>
            <td style="padding:8px;border:1px solid #ddd">{league.get('team_name','')}</td>
            <td style="padding:8px;border:1px solid #ddd">{league.get('rank','')}</td>
            <td style="padding:8px;border:1px solid #ddd">{league.get('wins','')}-{league.get('losses','')}</td>
            <td style="padding:8px;border:1px solid #ddd">{league.get('matchup_opponent','')}</td>
            <td style="padding:8px;border:1px solid #ddd">{league.get('my_points','')} vs {league.get('opp_points','')}</td>
        </tr>"""

    return f"""
    <html><body style="font-family:Arial,sans-serif;padding:20px">
    <h2 style="color:#6001d2">⚾ MLB Fantasy 週報</h2>
    <table style="border-collapse:collapse;width:100%">
        <thead>
            <tr style="background:#6001d2;color:white">
                <th style="padding:8px">聯盟</th>
                <th style="padding:8px">我的隊伍</th>
                <th style="padding:8px">排名</th>
                <th style="padding:8px">勝負</th>
                <th style="padding:8px">本週對手</th>
                <th style="padding:8px">本週分數</th>
            </tr>
        </thead>
        <tbody>{rows}</tbody>
    </table>
    <p style="color:#999;font-size:12px;margin-top:20px">自動產生 by MLB Fantasy Dashboard</p>
    </body></html>
    """

def send_weekly_report():
    sender = os.getenv("EMAIL_SENDER")
    password = os.getenv("EMAIL_PASSWORD")
    receiver = os.getenv("EMAIL_RECEIVER")

    if not all([sender, password, receiver]):
        print("Email not configured. Set EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER in .env")
        return

    tokens = load_tokens()
    if not tokens:
        print("No tokens saved. Login to the web app first.")
        return

    # Simulate session for API calls
    import flask
    app = flask.Flask(__name__)
    with app.app_context():
        with app.test_request_context():
            flask.session["access_token"] = tokens["access_token"]
            flask.session["refresh_token"] = tokens["refresh_token"]

            import yahoo_api as api
            try:
                leagues = api.get_user_leagues()
                leagues_data = []
                for league in leagues:
                    lk = league["league_key"]
                    my_team = api.get_my_team(lk)
                    if not my_team:
                        continue
                    matchup = api.get_current_matchup(my_team["team_key"])
                    opponent = ""
                    my_pts = ""
                    opp_pts = ""
                    if matchup and matchup.get("teams"):
                        for t in matchup["teams"]:
                            if t["team_key"] == my_team["team_key"]:
                                my_pts = t["points"]
                            else:
                                opponent = t["name"]
                                opp_pts = t["points"]
                    leagues_data.append({
                        "name": league["name"],
                        "team_name": my_team["name"],
                        "rank": my_team.get("rank", ""),
                        "wins": my_team.get("wins", ""),
                        "losses": my_team.get("losses", ""),
                        "matchup_opponent": opponent,
                        "my_points": my_pts,
                        "opp_points": opp_pts
                    })

                html = build_report_html(leagues_data)
                msg = MIMEMultipart("alternative")
                msg["Subject"] = "⚾ MLB Fantasy 週報"
                msg["From"] = sender
                msg["To"] = receiver
                msg.attach(MIMEText(html, "html"))

                with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                    server.login(sender, password)
                    server.sendmail(sender, receiver, msg.as_string())
                print("Weekly report sent!")
            except Exception as e:
                print(f"Report error: {e}")
