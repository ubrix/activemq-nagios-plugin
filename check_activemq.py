#!/usr/bin/env python
# -*- coding: utf-8 *-*

"""	Copyright 2015 predic8 GmbH, www.predic8.com

	Licensed under the Apache License, Version 2.0 (the "License");
	you may not use this file except in compliance with the License.
	You may obtain a copy of the License at

	http://www.apache.org/licenses/LICENSE-2.0

	Unless required by applicable law or agreed to in writing, software
	distributed under the License is distributed on an "AS IS" BASIS,
	WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
	See the License for the specific language governing permissions and
	limitations under the License. """

""" Project Home: https://github.com/predic8/activemq-nagios-plugin """
""" QueueAge      https://github.com/davidedg/activemq-nagios-plugin """

import argparse
import fnmatch
import os
import os.path as path
import json
import requests
import urllib
from datetime import datetime

import nagiosplugin as np

PLUGIN_VERSION = "0.8"
PREFIX = 'org.apache.activemq:'

## Dirty workaround for brokers with missing jars (java.lang.NoClassDefFoundError)
try:
    from html.parser import HTMLParser
except ImportError:
    from HTMLParser import HTMLParser

JSP_ERROR = None


class HTMLTableParser(HTMLParser):
    """ This class serves as a html table parser. It is able to parse multiple
    tables which you feed in. You can access the result per .tables field.
    """

    def __init__(
            self,
            decode_html_entities=False,
            data_separator=' ',
    ):

        HTMLParser.__init__(self)

        self._parse_html_entities = decode_html_entities
        self._data_separator = data_separator

        self._in_td = False
        self._in_th = False
        self._current_table = []
        self._current_row = []
        self._current_cell = []
        self.tables = []

    def handle_starttag(self, tag, attrs):
        """ We need to remember the opening point for the content of interest.
        The other tags (<table>, <tr>) are only handled at the closing point.
        """
        if tag == 'td':
            self._in_td = True
        if tag == 'th':
            self._in_th = True

    def handle_data(self, data):
        """ This is where we save content to a cell """
        if self._in_td or self._in_th:
            self._current_cell.append(data.strip())

    def handle_charref(self, name):
        """ Handle HTML encoded characters """

        if self._parse_html_entities:
            self.handle_data(self.unescape('&#{};'.format(name)))

    def handle_endtag(self, tag):
        """ Here we exit the tags. If the closing tag is </tr>, we know that we
        can save our currently parsed cells to the current table as a row and
        prepare for a new row. If the closing tag is </table>, we save the
        current table and prepare for a new one.
        """
        if tag == 'td':
            self._in_td = False
        elif tag == 'th':
            self._in_th = False

        if tag in ['td', 'th']:
            final_cell = self._data_separator.join(self._current_cell).strip()
            self._current_row.append(final_cell)
            self._current_cell = []
        elif tag == 'tr':
            self._current_table.append(self._current_row)
            self._current_row = []
        elif tag == 'table':
            self.tables.append(self._current_table)
            self._current_table = []


def make_url(args, dest):
    return (
        (args.jolokia_url + ('' if args.jolokia_url[-1] == '/' else '/') + dest)
        if args.jolokia_url
        else ('http://' + args.user + ':' + args.pwd
              + '@' + args.host + ':' + str(args.port)
              + '/' + args.url_tail + '/' + dest)
    )


def query_url(args, dest=''):
    return make_url(args, PREFIX + 'type=Broker,brokerName=' + args.brokerName + dest)


def queue_url(args, queue):
    return query_url(args, ',destinationType=Queue,destinationName=' + urllib.quote(queue))


def topic_url(args, topic):
    return query_url(args, ',destinationType=Topic,destinationName=' + urllib.quote(topic))


def health_url(args):
    return query_url(args, ',service=Health')


def loadJson(srcurl):
    try:
        r = requests.get(srcurl, timeout=get_timeout())
        return r.json() if r.status_code == requests.codes.ok else None
    except:
        return None


def queue_oldestmsg_timestamp_JSP(args, queue):
    #
    # Normally Jolokia API would return the oldest message with browseMessages()
    # Unfortunately Broker may be misconfigured with some missing JARs to interpret custom JMS Objects
    # While browsing the queue with OpenWire can get passed over these errors, using REST API would throw a (java.lang.NoClassDefFoundError)
    # If we detect this error, here we try a workaround: get the message from the BROWSE.JSP page of admin gui
    # It's really ugly and slow, but at least it produces some useful output
    #
    url = ("http://" + args.host + ":" + str(args.port) + "/admin/browse.jsp?JMSDestination=" + queue)
    headers = {'content-type': 'application/json'}
    r = requests.get(url, auth=(args.user, args.pwd), headers=headers, timeout=get_timeout())

    if r.status_code == requests.codes.ok:
        p = HTMLTableParser()
        p.feed(r.content.decode('utf-8'))
        if p.tables[0][0][6] == 'Timestamp':
            messages_tables = p.tables[0]
            del messages_tables[0]  ## delete header titles
            timestamps = sorted(list((datetime.strptime('%s' % t[6], '%Y-%m-%d %H:%M:%S:%f %Z') for t in
                                      messages_tables)))  ## get a sorted list of timestamps
            return timestamps[0] if timestamps else None

    raise Exception("Broker API error - tried with JSP but got no results")


def queue_oldestmsg_timestamp(args, queue):
    url = ("http://" + args.host + ":" + str(args.port) + "/api/jolokia/")
    params = {'maxDepth': '10', 'maxCollectionSize': '1', 'ignoreErrors': 'true'}
    headers = {'content-type': 'application/json'}
    data = {"type": "exec",
            "mbean": "org.apache.activemq:type=Broker,brokerName=localhost,destinationType=Queue,destinationName=" + queue,
            "operation": "browseMessages()"}
    r = requests.post(url, params=params, auth=(args.user, args.pwd), headers=headers, data=json.dumps(data),
                      timeout=get_timeout())

    global JSP_ERROR
    if r.status_code == requests.codes.ok:
        if r.json()['status'] == 200:
            msg_json = r.json()['value']
            msg_javats = msg_json[0]['jMSTimestamp'] if msg_json else 0
            msg_ts = datetime.utcfromtimestamp(msg_javats / 1000)
            return msg_ts if msg_json else None
        elif ((r.json()['status'] == 500) and (
                r.json()['error_type'] == 'java.lang.NoClassDefFoundError')):  # workaround with JSP page
            JSP_ERROR = r.json()['error']  # will report the missing class so broker can get a chance to be fixed
            return queue_oldestmsg_timestamp_JSP(args, queue)


def queueage(args):
    class ActiveMqQueueAgeContext(np.ScalarContext):

        def evaluate(self, metric, resource):
            if metric.value < 0:
                return self.result_cls(np.Unknown, metric=metric)

            # CRITICAL
            if metric.value >= self.critical.end:
                if args.queue:
                    return self.result_cls(np.Critical, 'Queue %s is %d minutes old (W=%d,C=%d)' %
                                           (args.queue, metric.value, self.warning.end, self.critical.end), metric)
                else:
                    return self.result_cls(np.Critical, 'Some queue is %d minutes old (W=%d,C=%d)' %
                                           (metric.value, self.warning.end, self.critical.end), metric)

            # WARNING
            if metric.value >= self.warning.end:
                if args.queue:
                    return self.result_cls(np.Warn, 'Queue %s is %d minutes old (W=%d,C=%d)' %
                                           (args.queue, metric.value, self.warning.end, self.critical.end), metric)
                else:
                    return self.result_cls(np.Warn, 'Some queue is %d minutes old (W=%d,C=%d)' %
                                           (metric.value, self.warning.end, self.critical.end), metric)

            # OK
            if args.queue:
                return self.result_cls(np.Ok, 'Queue %s is %d minutes old (W=%d,C=%d)' %
                                       (args.queue, metric.value, self.warning.end, self.critical.end), metric)
            else:
                return self.result_cls(np.Ok, 'All queues are younger than %d minutes (W=%d,C=%d)' %
                                       (self.warning.end, self.warning.end, self.critical.end), metric)

        def describe(self, metric):
            if metric.value < 0:
                return 'ERROR: ' + metric.name
            return super(ActiveMqQueueAgeContext, self).describe(metric)

        @staticmethod
        def fmt_violation(max_value):
            queue_name = args.queue if args.queue else '<some queues>'
            return 'Queue %s older than %d minutes' % (queue_name, max_value)

    class ActiveMqQueueAge(np.Resource):
        def __init__(self, pattern=None):
            self.pattern = pattern

        def probe(self):
            try:
                utcnow = datetime.utcnow()
                queues_json = loadJson(query_url(args))
                if not queues_json:
                    yield np.Metric('Getting Queue(s) FAILED: failed response', -1, context='age')
                    return
                for queue in queues_json['value']['Queues']:
                    queue_name = queue['objectName'].split(',')[1].split('=')[1]
                    if self.pattern and fnmatch.fnmatch(queue_name, self.pattern) or not self.pattern:
                        queue_oldest_time = queue_oldestmsg_timestamp(args, queue_name)
                        queue_oldest_time = queue_oldest_time if queue_oldest_time else utcnow
                        queue_age_minutes = int((utcnow - queue_oldest_time).total_seconds() // 60)

                        yield np.Metric('Minutes', queue_age_minutes, min=0, context='age')
            except IOError as e:
                yield np.Metric('Fetching network FAILED: ' + str(e), -1, context='age')
            except ValueError as e:
                yield np.Metric('Decoding Json FAILED: ' + str(e), -1, context='age')
            except KeyError as e:
                yield np.Metric('Getting Queue(s) FAILED: ' + str(e), -1, context='age')
            except Exception as e:
                yield np.Metric('Unexpected Error: ' + str(e), -1, context='age')

    class ActiveMqQueueAgeSummary(np.Summary):
        def ok(self, results):
            return results.first_significant.hint + (
                ' BUT values retrieved via JSP pages due to: %s' % JSP_ERROR if JSP_ERROR else '')

        def problem(self, results):
            return results.first_significant.hint + (
                ' and values retrieved via JSP pages due to: %s' % JSP_ERROR if JSP_ERROR else '') if results.first_significant.hint else "Could not retrieve data"

    np.Check(
        ActiveMqQueueAge(args.queue) if args.queue else ActiveMqQueueAge(),
        ActiveMqQueueAgeContext('age', args.warn, args.crit),
        ActiveMqQueueAgeSummary()
    ).main(timeout=get_timeout())


def queuesize(args):
    class ActiveMqQueueSizeContext(np.ScalarContext):
        def evaluate(self, metric, resource):
            if metric.value < 0:
                return self.result_cls(np.Unknown, metric=metric)

            if metric.value >= self.critical.end:
                return self.result_cls(np.Critical, ActiveMqQueueSizeContext.fmt_violation(self.critical.end), metric)

            if metric.value >= self.warning.end:
                return self.result_cls(np.Warn, ActiveMqQueueSizeContext.fmt_violation(self.warning.end), metric)

            return self.result_cls(np.Ok, None, metric)

        def describe(self, metric):
            if metric.value < 0:
                return 'ERROR: ' + metric.name
            return super(ActiveMqQueueSizeContext, self).describe(metric)

        @staticmethod
        def fmt_violation(max_value):
            return 'Queue size is greater than or equal to %d' % max_value

    class ActiveMqQueueSize(np.Resource):
        def __init__(self, pattern=None):
            self.pattern = pattern

        def probe(self):
            try:
                queues_json = loadJson(query_url(args))
                if not queues_json:
                    yield np.Metric('Getting Queue(s) FAILED: failed response', -1, context='size')
                    return
                for queue in queues_json['value']['Queues']:
                    queue_name = queue['objectName'].split(',')[1].split('=')[1]
                    if (self.pattern and fnmatch.fnmatch(queue_name, self.pattern)
                            or not self.pattern):
                        qJ = loadJson(make_url(args, queue['objectName']))['value']
                        yield np.Metric('Queue Size of %s' % queue_name,
                                        qJ['QueueSize'], min=0, context='size')
            except IOError as e:
                yield np.Metric('Fetching network FAILED: ' + str(e), -1, context='size')
            except ValueError as e:
                yield np.Metric('Decoding Json FAILED: ' + str(e), -1, context='size')
            except KeyError as e:
                yield np.Metric('Getting Queue(s) FAILED: ' + str(e), -1, context='size')

    class ActiveMqQueueSizeSummary(np.Summary):
        def ok(self, results):
            if len(results) > 1:
                lenQ = str(len(results))
                minQ = str(min([r.metric.value for r in results]))
                avgQ = str(sum([r.metric.value for r in results]) / len(results))
                maxQ = str(max([r.metric.value for r in results]))
                return ('Checked ' + lenQ + ' queues with lengths min/avg/max = '
                        + '/'.join([minQ, avgQ, maxQ]))
            else:
                return super(ActiveMqQueueSizeSummary, self).ok(results)

    np.Check(
        ActiveMqQueueSize(args.queue) if args.queue else ActiveMqQueueSize(),
        ActiveMqQueueSizeContext('size', args.warn, args.crit),
        ActiveMqQueueSizeSummary()
    ).main(timeout=get_timeout())


# when debugging the application, set the TIMEOUT env variable to 0 to disable the timeout during check execution
def get_timeout():
    return int(os.environ.get('TIMEOUT')) if 'TIMEOUT' in os.environ else 10


def health(args):
    class ActiveMqHealthContext(np.Context):
        def evaluate(self, metric, resource):
            if metric.value < 0:
                return self.result_cls(np.Unknown, metric=metric)
            if metric.value == "Good":
                return self.result_cls(np.Ok, metric=metric)
            else:
                return self.result_cls(np.Warn, metric=metric)

        def describe(self, metric):
            if metric.value < 0:
                return 'ERROR: ' + metric.name
            return metric.name + ' ' + metric.value

    class ActiveMqHealth(np.Resource):
        def probe(self):
            try:
                status = loadJson(health_url(args))['value']['CurrentStatus']
                return np.Metric('CurrentStatus', status, context='health')
            except IOError as e:
                return np.Metric('Fetching network FAILED: ' + str(e), -1, context='health')
            except ValueError as e:
                return np.Metric('Decoding Json FAILED: ' + str(e), -1, context='health')
            except KeyError as e:
                return np.Metric('Getting Values FAILED: ' + str(e), -1, context='health')

    np.Check(
        ActiveMqHealth(),  ## check ONE queue
        ActiveMqHealthContext('health')
    ).main(timeout=get_timeout())


def subscriber(args):
    """ There are several internal error codes for the subscriber module:
		-1   Miscellaneous Error (network, json, key value)
		-2   Topic Name is invalid / doesn't exist
		-3   Topic has no Subscribers
		-4   Client ID is invalid / doesn't exist
		True/False aren't error codes, but the result whether clientId is an
		active subscriber of topic.
	"""

    class ActiveMqSubscriberContext(np.Context):
        def evaluate(self, metric, resource):
            if metric.value == -1:  # Network or JSON Error
                return self.result_cls(np.Unknown, metric=metric)
            elif metric.value == -2:  # Topic doesn't exist
                return self.result_cls(np.Critical, metric=metric)
            elif metric.value == -3:  # Topic has no subscribers
                return self.result_cls(np.Critical, metric=metric)
            elif metric.value == -4:  # Client invalid
                return self.result_cls(np.Critical, metric=metric)
            elif metric.value == True:
                return self.result_cls(np.Ok, metric=metric)
            elif metric.value == False:
                return self.result_cls(np.Warn, metric=metric)
            else:  ###
                return self.result_cls(np.Critical, metric=metric)

        def describe(self, metric):
            if metric.value == -1:
                return 'ERROR: ' + metric.name
            elif metric.value == -2:
                return 'Topic ' + args.topic + ' IS INVALID / DOES NOT EXIST'
            elif metric.value == -3:
                return 'Topic ' + args.topic + ' HAS NO SUBSCRIBERS'
            elif metric.value == -4:
                return 'Subscriber ID ' + args.clientId + ' IS INVALID / DOES NOT EXIST'
            return ('Client ' + args.clientId + ' is an '
                    + ('active' if metric.value == True else 'INACTIVE')
                    + ' subscriber of Topic ' + args.topic)

    class ActiveMqSubscriber(np.Resource):
        def probe(self):
            try:
                resp = loadJson(topic_url(args, args.topic))

                if resp['status'] != 200:  # None -> Topic doesn't exist
                    return np.Metric('subscription', -2, context='subscriber')

                subs = resp['value']['Subscriptions']  # Subscriptions for Topic

                def client_is_active_subscriber(subscription):
                    subUrl = make_url(args, urllib.quote(subscription['objectName']))
                    subResp = loadJson(subUrl)  # get the subscription

                    if subResp['value']['DestinationName'] != args.topic:  # should always hold
                        return -2  # Topic is invalid / doesn't exist
                    if subResp['value']['ClientId'] != args.clientId:  # subscriber ID check
                        return -4  # clientId invalid
                    return subResp['value']['Active']  # subscribtion active?

                # check if clientId is among the subscribers
                analyze = [client_is_active_subscriber(s) for s in subs]
                if not analyze:
                    return np.Metric('subscription', -3, context='subscriber')
                if -2 in analyze:  # should never occur, just for safety
                    return np.Metric('subscription', -2, context='subscriber')
                elif True in analyze:  # active subscriber
                    return np.Metric('subscription', True, context='subscriber')
                elif False in analyze:  # INACTIVE subscriber
                    return np.Metric('subscription', False, context='subscriber')
                elif -4 in analyze:  # all clients failed
                    return np.Metric('subscription', -4, context='subscriber')

            except IOError as e:
                return np.Metric('Fetching network FAILED: ' + str(e), -1, context='subscriber')
            except ValueError as e:
                return np.Metric('Decoding Json FAILED: ' + str(e), -1, context='subscriber')
            except KeyError as e:
                return np.Metric('Getting Values FAILED: ' + str(e), -1, context='subscriber')

    np.Check(
        ActiveMqSubscriber(),
        ActiveMqSubscriberContext('subscriber')
    ).main(timeout=get_timeout())


def exists(args):
    class ActiveMqExistsContext(np.Context):
        def evaluate(self, metric, resource):
            if metric.value < 0:
                return self.result_cls(np.Unknown, metric=metric)
            if metric.value > 0:
                return self.result_cls(np.Ok, metric=metric)
            return self.result_cls(np.Critical, metric=metric)

        def describe(self, metric):
            if metric.value < 0:
                return 'ERROR: ' + metric.name
            if metric.value == 0:
                return 'Neither Queue nor Topic with name ' + args.name + ' were found!'
            if metric.value == 1:
                return 'Found Queue with name ' + args.name
            if metric.value == 2:
                return 'Found Topic with name ' + args.name
            return super(ActiveMqExistsContext, self).describe(metric)

    class ActiveMqExists(np.Resource):
        def probe(self):
            try:
                respQ = loadJson(queue_url(args, args.name))
                if respQ['status'] == 200:
                    return np.Metric('exists', 1, context='exists')

                respT = loadJson(topic_url(args, args.name))
                if respT['status'] == 200:
                    return np.Metric('exists', 2, context='exists')

                return np.Metric('exists', 0, context='exists')

            except IOError as e:
                return np.Metric('Network fetching FAILED: ' + str(e), -1, context='exists')
            except ValueError as e:
                return np.Metric('Decoding Json FAILED: ' + str(e), -1, context='exists')
            except KeyError as e:
                return np.Metric('Getting Queue(s) FAILED: ' + str(e), -1, context='exists')

    np.Check(
        ActiveMqExists(),
        ActiveMqExistsContext('exists')
    ).main(timeout=get_timeout())


def subscriber_pending(args):
    """ Mix from queuesize and subscriber check.
		Check that the given clientId is a subscriber of the given Topic.
		Also check
	"""

    class ActiveMqSubscriberPendingContext(np.ScalarContext):
        def evaluate(self, metric, resource):
            if metric.value < 0:
                return self.result_cls(np.Critical, metric=metric)
            return super(ActiveMqSubscriberPendingContext, self).evaluate(metric, resource)

        def describe(self, metric):
            if metric.value < 0:
                return 'ERROR: ' + metric.name
            return super(ActiveMqSubscriberPendingContext, self).describe(metric)

    class ActiveMqSubscriberPending(np.Resource):
        def probe(self):
            try:
                resp = loadJson(query_url(args))
                subs = (resp['value']['TopicSubscribers'] +
                        resp['value']['InactiveDurableTopicSubscribers'])
                for sub in subs:
                    qJ = loadJson(make_url(args, sub['objectName']))['value']
                    if not qJ['SubscriptionName'] == args.subscription:
                        continue  # skip subscriber
                    if not qJ['ClientId'] == args.clientId:
                        # When this if is entered, we have found the correct
                        # subscription, but the clientId doesn't match
                        return np.Metric('ClientId error: Expected: %s. Got: %s'
                                         % (args.clientId, qJ['ClientId']),
                                         -1, context='subscriber_pending')
                    return np.Metric('Pending Messages for %s' % qJ['SubscriptionName'],
                                     qJ['PendingQueueSize'], min=0,
                                     context='subscriber_pending')
            except IOError as e:
                return np.Metric('Fetching network FAILED: ' + str(e), -1, context='subscriber_pending')
            except ValueError as e:
                return np.Metric('Decoding Json FAILED: ' + str(e), -1, context='subscriber_pending')
            except KeyError as e:
                return np.Metric('Getting Subscriber FAILED: ' + str(e), -1, context='subscriber_pending')

    np.Check(
        ActiveMqSubscriberPending(),
        ActiveMqSubscriberPendingContext('subscriber_pending', args.warn, args.crit),
    ).main(timeout=get_timeout())


def dlq(args):
    class ActiveMqDlqScalarContext(np.ScalarContext):
        def evaluate(self, metric, resource):
            if metric.value > 0:
                return self.result_cls(np.Critical, metric=metric)
            else:
                return self.result_cls(np.Ok, metric=metric)

    class ActiveMqDlq(np.Resource):
        def __init__(self, prefix, cachedir):
            super(ActiveMqDlq, self).__init__()
            self.cache = None
            self.cachedir = path.join(path.expanduser(cachedir), 'activemq-nagios-plugin')
            self.cachefile = path.join(self.cachedir, 'dlq-cache.json')
            self.parse_cache()
            self.prefix = prefix

        def parse_cache(self):  # deserialize
            if not os.path.exists(self.cachefile):
                self.cache = {}
            else:
                with open(self.cachefile, 'r') as cachefile:
                    self.cache = json.load(cachefile)

        def write_cache(self):  # serialize
            if not os.path.exists(self.cachedir):
                os.makedirs(self.cachedir)
            with open(self.cachefile, 'w') as cachefile:
                json.dump(self.cache, cachefile)

        def probe(self):
            try:
                for queue in loadJson(query_url(args))['value']['Queues']:
                    qJ = loadJson(make_url(args, queue['objectName']))['value']
                    if qJ['Name'].startswith(self.prefix):
                        oldcount = self.cache.get(qJ['Name'])

                        if oldcount == None:
                            more = 0
                            msg = 'First check for DLQ'
                        else:
                            assert isinstance(oldcount, int)
                            more = qJ['QueueSize'] - oldcount
                            if more == 0:
                                msg = 'No additional messages in'
                            elif more > 0:
                                msg = 'More messages in'
                            else:  # more < 0
                                msg = 'Less messages in'
                        self.cache[qJ['Name']] = qJ['QueueSize']
                        self.write_cache()
                        yield np.Metric(msg + ' %s' % qJ['Name'],
                                        more, context='dlq')
            except IOError as e:
                yield np.Metric('Fetching network FAILED: ' + str(e), -1, context='dlq')
            except ValueError as e:
                yield np.Metric('Decoding Json FAILED: ' + str(e), -1, context='dlq')
            except KeyError as e:
                yield np.Metric('Getting Queue(s) FAILED: ' + str(e), -1, context='dlq')

    class ActiveMqDlqSummary(np.Summary):
        def ok(self, results):
            if len(results) > 1:
                lenQ = str(len(results))
                bigger = str(len([r.metric.value for r in results if r.metric.value > 0]))
                return ('Checked ' + lenQ + ' DLQs of which ' + bigger + ' contain additional messages.')
            else:
                return super(ActiveMqDlqSummary, self).ok(results)

    np.Check(
        ActiveMqDlq(args.prefix, args.cachedir),
        ActiveMqDlqScalarContext('dlq'),
        ActiveMqDlqSummary()
    ).main(timeout=get_timeout())


def add_warn_crit(parser, what):
    parser.add_argument('-w', '--warn',
                        metavar='WARN', type=int, default=10,
                        help='Warning if ' + what + ' is greater than or equal to. (default: %(default)s)')
    parser.add_argument('-c', '--crit',
                        metavar='CRIT', type=int, default=100,
                        help='Warning if ' + what + ' is greater than or equal to. (default: %(default)s)')


@np.guarded
def main():
    # Top-level Argument Parser & Subparsers Initialization
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument('-v', '--version', action='version',
                        help='Print version number',
                        version='%(prog)s version ' + str(PLUGIN_VERSION)
                        )

    connection = parser.add_argument_group('Connection')
    connection.add_argument('--host', default='localhost',
                            help='ActiveMQ Server Hostname (default: %(default)s)')
    connection.add_argument('--port', type=int, default=8161,
                            help='ActiveMQ Server Port (default: %(default)s)')
    connection.add_argument('-b', '--brokerName', default='localhost',
                            help='Name of your broker. (default: %(default)s)')
    connection.add_argument('--url-tail',
                            default='api/jolokia/read',
                            # default='hawtio/jolokia/read',
                            help='Jolokia URL tail part. (default: %(default)s)')
    connection.add_argument('-j', '--jolokia-url',
                            help='''Override complete Jolokia URL.
				(Default: "http://USER:PWD@HOST:PORT/URLTAIL/").
				The parameters --user, --pwd, --host and --port are IGNORED
				if this parameter is specified!
				Please set this parameter carefully as it essential
				for the program to work properly and is not validated.''')

    credentials = parser.add_argument_group('Credentials')
    credentials.add_argument('-u', '--user', default='admin',
                             help='Username for ActiveMQ admin account. (default: %(default)s)')
    credentials.add_argument('-p', '--pwd', default='admin',
                             help='Password for ActiveMQ admin account. (default: %(default)s)')

    subparsers = parser.add_subparsers()

    # Sub-Parser for queueage
    parser_queueage = subparsers.add_parser('queueage',
                                            help="""Check QueueAge: This mode checks the queue age of one
				or more queues on the ActiveMQ server.
				You can specify a queue name to check (even a pattern);
				see description of the 'queue' parameter for details.""")
    add_warn_crit(parser_queueage, 'Queue Age')
    parser_queueage.add_argument('queue', nargs='?',
                                 help='''Name of the Queue that will be checked.
				If left empty, all Queues will be checked.
				This also can be a Unix shell-style Wildcard
				(much less powerful than a RegEx)
				where * and ? can be used.''')
    parser_queueage.set_defaults(func=queueage)

    # Sub-Parser for queuesize
    parser_queuesize = subparsers.add_parser('queuesize',
                                             help="""Check QueueSize: This mode checks the queue size of one
				or more queues on the ActiveMQ server.
				You can specify a queue name to check (even a pattern);
				see description of the 'queue' parameter for details.""")
    add_warn_crit(parser_queuesize, 'Queue Size')
    parser_queuesize.add_argument('queue', nargs='?',
                                  help='''Name of the Queue that will be checked.
				If left empty, all Queues will be checked.
				This also can be a Unix shell-style Wildcard
				(much less powerful than a RegEx)
				where * and ? can be used.''')
    parser_queuesize.set_defaults(func=queuesize)

    # Sub-Parser for health
    parser_health = subparsers.add_parser('health',
                                          help="""Check Health: This mode checks if the current status is 'Good'.""")
    # no additional arguments necessary
    parser_health.set_defaults(func=health)

    # Sub-Parser for subscriber
    parser_subscriber = subparsers.add_parser('subscriber',
                                              help="""Check Subscriber: This mode checks if the given 'clientId'
				is a subscriber of the specified 'topic'.""")
    parser_subscriber.add_argument('--clientId', required=True,
                                   help='Client ID of the client that will be checked')
    parser_subscriber.add_argument('--topic', required=True,
                                   help='Name of the Topic that will be checked.')
    parser_subscriber.set_defaults(func=subscriber)

    # Sub-Parser for exists
    parser_exists = subparsers.add_parser('exists',
                                          help="""Check Exists: This mode checks if a Queue or Topic with the
				given name exists.
				If either a Queue or a Topic with this name exist,
				this mode yields OK.""")
    parser_exists.add_argument('--name', required=True,
                               help='Name of the Queue or Topic that will be checked.')
    parser_exists.set_defaults(func=exists)

    # Sub-Parser for queuesize-subscriber
    parser_subscriber_pending = subparsers.add_parser('subscriber-pending',
                                                      help="""Check Subscriber-Pending:
				This mode checks that the given subscriber doesn't have
				too many pending messages (specified with -w and -c)
				and that the given clientId the Id that is involved in
				the subscription.""")
    parser_subscriber_pending.add_argument('--subscription', required=True,
                                           help='Name of the subscription thath will be checked.')
    parser_subscriber_pending.add_argument('--clientId', required=True,
                                           help='The ID of the client that is involved in the specified subscription.')
    add_warn_crit(parser_subscriber_pending, 'Pending Messages')
    parser_subscriber_pending.set_defaults(func=subscriber_pending)

    # Sub-Parser for dlq
    parser_dlq = subparsers.add_parser('dlq',
                                       help="""Check DLQ (Dead Letter Queue):
				This mode checks if there are new messages in DLQs
				with the specified prefix.""")
    parser_dlq.add_argument('--prefix',  # required=False,
                            default='ActiveMQ.DLQ.',
                            help='DLQ prefix to check. (default: %(default)s)')
    parser_dlq.add_argument('--cachedir',  # required=False,
                            default='~/.cache',
                            help='DLQ cache base directory. (default: %(default)s)')
    add_warn_crit(parser_dlq, 'DLQ Queue Size')
    parser_dlq.set_defaults(func=dlq)

    # Evaluate Arguments
    args = parser.parse_args()
    # call the determined function with the parsed arguments
    try:
        args.func(args)
    except AttributeError:  # https://bugs.python.org/issue16308
        parser.print_help()
        exit(3)


if __name__ == '__main__':
    main()
