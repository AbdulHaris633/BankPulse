import time
import requests
from pprint import pprint

from app.services.logger import error, init_logger

from_time = '-10m'

loggly_url = 'https://aesnetics.loggly.com/apiv2/{}'
search_endpoint = loggly_url.format('search')
events_endpoint = loggly_url.format('events')
loggly_token = ''

headers = {'Authorization': 'bearer {}'.format(loggly_token)}


def get_events(seen_events: list) -> [bool, list]:
    try:
        params = {'q': 'json.level:ERROR',
                  'from': from_time,
                  'until': 'now'}
        resp = requests.get(url=search_endpoint, headers=headers, params=params)
        if not resp.ok:
            raise Exception("Cannot fetch logs")
        rsid = resp.json()["rsid"]["id"]
        params = {'rsid': rsid}
        resp = requests.get(url=events_endpoint, headers=headers, params=params)
        if not resp.ok:
            raise Exception("Cannot fetch logs")
        json_resp = resp.json()
        events = json_resp["events"]
        parsed_events = []
        for event in events:
            try:
                event_data: dict = event["event"]
                if "json" in event_data.keys():
                    json: dict = event_data["json"]
                    msg = json["msg"]
                    pid = json["pid"]
                    event_descriptor = {'pid': pid, 'timestamp': event["timestamp"]}
                    if event_descriptor in seen_events:
                        continue
                    timestamp = json["timestamp"]
                    function = json["function"]
                    line_no = json["line_no"]
                    file_name = json["file_name"]
                    data = None
                    screenshot_url = None
                    if "data" in json.keys():
                        data: dict = json["data"]
                    if "screenshot_url" in json.keys():
                        screenshot_url = json["screenshot_url"]
                    parsed_event = {'pid': pid,
                                    'msg': msg,
                                    'timestamp': timestamp,
                                    'file_name': file_name,
                                    'function': function,
                                    'line_no': line_no,
                                    'data': data,
                                    'screenshot_url': screenshot_url}
                    parsed_events.append(parsed_event)
                for parsed_event in parsed_events:
                    pprint(parsed_event)
            except:
                pass
        return parsed_events
    except Exception as exception:
        error("Could not fetch events")
        error(exception)
        return False


if __name__ == '__main__':
    init_logger()
    seen_events = []
    while True:
        try:
            get_events(seen_events)
        except Exception as e:
            error(e)
        finally:
            time.sleep(60)
