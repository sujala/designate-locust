from locust import HttpLocust
from locust import TaskSet
from locust import task
import locust.events
from faker import Factory
import json
import datetime
import random
import redis

import client
from client import DesignateClient
from config import Config

# all of our flask web routing functions need to be in this module
from web import *

CONFIG = Config(json_file='config.json')


def get_timestamp():
    return str(datetime.datetime.now())

def randomize(string):
    return "{0}{1}".format(string, random.randint(1000000000, 9999999999))

def random_ip():
    return ".".join(str(random.randint(0, 255)) for _ in range(4))

def select_random_item(vals):
    """Return a random item in this list, or None if empty."""
    if vals:
        return random.choice(vals)
    return None


class RedisBuffer(list):
    """Serves a buffer for data written to Redis. The buffer is flushed
    whenever the length of this list reaches the max_size (but this is
    only works when using append() method)"""

    CREATE = 1
    UPDATE = 2

    def __init__(self, client, max_size=100):
        self.client = client
        self.max_size = max_size

    def append(self, item):
        super(RedisBuffer, self).append(item)
        if len(self) >= self.max_size:
            self.flush()

    def flush(self):
        """Write everything stored in this buffer to redis."""
        print "Flushing buffer -- {0} items to redis".format(len(self))
        while self:
            item = self.pop()
            if not self._store_item(item):
                print "Failed to store: ", item[0], item[1].json()

    def _store_item(self, item):
        """Store a key-value pair in redis where the key contains the
        zone name and serial number, and the value is the timestamp of the
        create or update corresponding to that serial."""
        # print "Store: ", item[0], item[1].json()
        type, response = item

        response_json = response.json().get('zone')
        zone_name = response_json['name']
        serial = response_json['serial']
        response_json_rec = response.json().get('recordset')

        key = "api-{0}-{1}".format(zone_name, serial)
        if type == self.CREATE:
            value = response_json['created_at']
        elif type == self.UPDATE:
            value = response_json['updated_at']
        else:
            print "ERORORORR"
            return False
        return self.client.set(key, value)


class MyTaskSet(TaskSet):

    def __init__(self, *args, **kwargs):
        super(MyTaskSet, self).__init__(*args, **kwargs)
        self.designate_client = DesignateClient(self.client)
        self.fake = Factory.create()
        # initialize redis client
        self.redis_client = redis.StrictRedis(
            host=CONFIG.redis_host,
            port=CONFIG.redis_port,
            password=CONFIG.redis_password,
            db=0)
        # ping redis to ensure the connection is good
        self.redis_client.ping()

        # the RedisBuffer will write to Redis periodically
        self.buffer = RedisBuffer(client=self.redis_client)

        # stores all created zones
        self.zone_list = []

        # a pool of tenants from which zones will be created
        #self.tenant_list = [chr(i) for i in xrange(ord('A'), ord('Z') + 1)]
        n_tenants = CONFIG.n_tenants or 1
        print "Using {0} tenants".format(n_tenants)
        self.tenant_list = ["T{0}".format(i + 1) for i in xrange(n_tenants)]

        # ensure we don't reach quota limits
        # this doesn't seem to work...
        #locust.events.master_start_hatching += lambda: self.increase_quotas()
        #locust.events.master_stop_hatching += lambda: self.increase_quotas()
        # ensure cleanup when the test is stopped
        locust.events.locust_stop_hatching += lambda: self.on_stop()
        # ensure cleanup on interrupts
        locust.events.quitting += lambda: self.on_stop()

    def _get_random_tenant(self):
        return select_random_item(self.tenant_list)

    def on_start(self):
        """This method is run whenever a simulated user starts this TaskSet."""
        self.increase_quotas()

    def increase_quotas(self):
        """This only needs to be run on the master."""
        # ensure we won't reach quota limits
        payload = { "quota": { "zones": 999999999,
                               "recordset_records": 999999999,
                               "zone_records": 999999999,
                               "zone_recordsets": 999999999}}
        for tenant in self.tenant_list:
            print "Increasing quotas for tenant {0}".format(tenant)
            headers = { client.ROLE_HEADER: 'admin',
                        client.PROJECT_ID_HEADER: tenant}
            self.designate_client.patch_quotas(tenant=tenant,
                                               data=json.dumps(payload),
                                               headers=headers,
                                               name='/v2/quotas/tenantID')

    def on_stop(self):
        print "calling on_stop"
        # write all data to redis
        self.buffer.flush()

    @task
    def zone_post(self):
        name = randomize(self.fake.first_name())
        zone = "{0}.com.".format(name)
        email = self.fake.email()

        payload = {"zone": { "name": zone,
                             "email": email,
                             "ttl": 7200} }

        # pick a random tenant
        headers = { client.ROLE_HEADER: 'admin',
                    client.PROJECT_ID_HEADER: self._get_random_tenant() }

        response = self.designate_client.post_zone(data=json.dumps(payload),
                                                   name='/zones',
                                                   headers=headers)

        if response.status_code == 201:
            self.buffer.append((self.buffer.CREATE, response))
            self.zone_list.append(response)

    @task
    def zone_patch(self):
        response = select_random_item(self.zone_list)
        if response is None:
            return

        response_json = response.json().get('zone')
        # print "response_json", response_json
        zone_id = response_json['id']
        project_id = response_json['project_id']

        payload = {"zone": { "ttl": 3600 } }

        headers = { client.ROLE_HEADER: 'admin',
                    client.PROJECT_ID_HEADER: project_id }

        update_resp = self.designate_client.patch_zone(
            zone_id, data=json.dumps(payload), name='/zone/zoneID',
            headers=headers)
        if update_resp.status_code == 200:
            self.buffer.append((self.buffer.UPDATE, update_resp))

    @task
    def recordset_create(self):
        response = select_random_item(self.zone_list)
        if response is None:
            return

        response_json = response.json().get('zone')
        # print "response_json", response_json
        zone_id = response_json['id']
        zone_name = response_json['name']
        project_id = response_json['project_id']

        a_record_name = "{0}.{1}".format(randomize("www"), zone_name)
        payload = {"recordset" : {"name" : a_record_name,
                                  "type" : "A",
                                  "ttl" : 3600,
                                  "records" : [ random_ip() ] }}

        headers = { client.ROLE_HEADER: 'admin',
                    client.PROJECT_ID_HEADER: project_id }

        recordset_resp = self.designate_client.post_recordset(
            zone_id, data=json.dumps(payload), name='/zones/zoneID/recordsets',
            headers=headers)

        # store the updated zone's response which contains the updated serial
        if recordset_resp.status_code == 201:
            zone_resp = self.designate_client.get_zone(zone_id,
                                                       name='/zone/zoneID',
                                                       headers=headers)
            if zone_resp.status_code == 200:
                # print "adding zone_resp:", zone_resp.json()
                self.buffer.append((self.buffer.UPDATE, zone_resp))


class MyLocust(HttpLocust):
    task_set = MyTaskSet
    # in milliseconds
    min_wait = CONFIG.min_wait or 100
    max_wait = CONFIG.max_wait or 1000

    host = CONFIG.designate_host
