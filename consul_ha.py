import json
import urllib2
import socket
import sys
import time
import re
from base64 import b64decode

class ConsulHa:
    def __init__(self, service_name, lock_delay):
        self.service_name = service_name
        self.hostname = socket.gethostname()
        self.lock_delay = lock_delay
        self.session_id = None

    def init_consul_session(self):
        try:
            session_list = json.loads(urllib2.urlopen("http://localhost:8500/v1/session/list").read())
            session_name = "%s-%s" % (self.hostname, self.service_name)
        except urllib2.URLError as e:
            print("Consul is not running at localhost:8500")
            return
        except urllib2.HTTPError as e:
            print("Warning: error connect with consul: %s" % e)
            return

        for session_hash in session_list:
            if session_hash["Name"] == session_name:
                return session_hash["ID"]

        session_data = {
                "Name": session_name,
                "LockDelay": "%ss" % self.lock_delay,
                "Checks": [
                    "serfHealth",
                    "service:%s" % self.service_name
                    ]
                }

        try:
            opener = urllib2.build_opener(urllib2.HTTPHandler)
            request = urllib2.Request('http://localhost:8500/v1/session/create', data=json.dumps(session_data))
            request.get_method = lambda: 'PUT'
            session_response = opener.open(request).read()

            return json.loads(session_response)["ID"]
        except urllib2.HTTPError as e:
            print("WARNING: cannot create session until check is healthy: %s" % e)
            return

    def acquire_session_lock(self):
        try:
            opener = urllib2.build_opener(urllib2.HTTPHandler)
            request = urllib2.Request('http://localhost:8500/v1/kv/service/%s/leader?acquire=%s' % (self.service_name, self.session_id), data=self.hostname)
            request.get_method = lambda: 'PUT'
            return opener.open(request).read() == "true"
        except urllib2.HTTPError as e:
            if re.search('Invalid session', e.read()):
                self.session_id = None
            else:
                print("WARNING: error connecting with consul: %s" % e)
            return False

    def release_session_lock(self):
        opener = urllib2.build_opener(urllib2.HTTPHandler)
        request = urllib2.Request('http://localhost:8500/v1/kv/service/%s/leader?release=%s' % (self.service_name, self.session_id), data=self.hostname)
        request.get_method = lambda: 'PUT'
        opener.open(request)

    def is_unlocked(self):
        try:
            response = json.loads(urllib2.urlopen('http://localhost:8500/v1/kv/service/%s/leader' % self.service_name).read())
            response[0]["Session"]
            return False
        except urllib2.HTTPError as e:
            if e.code == 404:
                return True
            return False
        except KeyError as e:
            return True

    def has_lock(self):
        try:
            endpoint_hash = json.loads(urllib2.urlopen('http://localhost:8500/v1/kv/service/%s/leader' % self.service_name).read())
            return endpoint_hash[0]["Session"] == self.session_id
        except Exception as e:
            return False

    def fetch_current_leader(self):
        try:
            endpoint_hash = json.loads(urllib2.urlopen("http://localhost:8500/v1/kv/service/%s/leader" % self.service_name).read())
            node_name     = b64decode(endpoint_hash[0]["Value"])
            return json.loads(urllib2.urlopen("http://localhost:8500/v1/catalog/node/%s" % node_name).read())
        except urllib2.HTTPError:
            return None

    def fetch_members_list(self):
        return json.loads(urllib2.urlopen("http://localhost:8500/v1/health/service/%s" % self.service_name).read())

    def run_cycle(self):
        self.session_id = self.session_id or self.init_consul_session()

        if self.session_id == None:
            print "session_id was not created properly"
            return False

        if self.is_unlocked():
            if self.state_handler.is_healthiest_node():
                if self.acquire_session_lock():
                    if not self.state_handler.is_leader():
                        self.state_handler.promote()
                        return "promoted self to leader by acquiring session lock"

                    return "acquired session lock as a leader"
                else:
                    if self.state_handler.is_leader():
                        self.state_handler.demote(self.fetch_current_leader())
                        return "demoted self due after trying and failing to obtain lock"
                    else:
                        self.state_handler.follow_the_leader(self.fetch_current_leader())
                        return "following new leader after trying and failing to obtain lock"
            else:
                if self.state_handler.is_leader():
                    self.state_handler.demote(self.fetch_current_leader())
                    return "demoting self because i am not the healthiest node"
                else:
                    self.state_handler.follow_the_leader(self.fetch_current_leader())
                    return "following a different leader because i am not the healthiest node"

        else:
            if self.has_lock():
                if not self.state_handler.is_leader():
                    self.state_handler.promote()
                    return "promoted self to leader because i had the session lock"
                else:
                    return "no action.  i am the primary with the lock"
            else:
                if self.state_handler.is_leader():
                    self.state_handler.demote(self.fetch_current_leader())
                    return "demoting self because i do not have the lock and i was a primary"
                else:
                    self.state_handler.follow_the_leader(self.fetch_current_leader())
                    return "no action.  i am a secondary and i am following a leader"

    def run(self):
        while True:
            self.run_cycle()
            time.sleep(10)
