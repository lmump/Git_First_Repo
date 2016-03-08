'''
Rational Team Concert
'''
import json, os, os.path, pycurl, re, StringIO, subprocess, sys, time, urllib
import pdb
import logging as log
import xml.dom.minidom as minidom
from pprint import *
from retry import retry

class FileReader:
	'Helper class to supply libcurl read function callbacks'
	def __init__(self, fp):
		self.fp = fp
	def read_callback(self, size):
		chunk = self.fp.read(size)
		return str(chunk)

class RTCError(Exception):
	pass

class Server(object):
	'''
	Jazz Team Server hostname, port, and RTC(?) version.

	This class does not make a lot of sense, unless it is effectively shared
	between, say, CLI and RTC. Otherwise, it ought to be subsumed by RTC.
	'''
	def __init__(self, host='cornet-ccm1.cisco.com', port=9443, root=None, version=3):
		if not host:
			raise RTCError("%s requires a hostname, none provided" % str(self.__class__))
		(self.host, self.port, self.version) = (host, port, version)
		self.url = 'https://%s:%s/%s' % (self.host, self.port, root)

class CLI(object):
	def __init__(self, server, user, password, scm='lscm'):
		self.server = server
		self.user = user
		self.password = password
		self.scm = scm
	@retry(RTCError, tries=9, delay=8, backoff=2, logger=log)
	def execute(self, cmd, scm_opts=''):
		cl = '%s %s %s' % (self.scm, scm_opts, cmd)
		log.info('starting RTC CLI command: %s' % cl)
		p = subprocess.Popen(cl, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		o, e = p.communicate()
		if p.returncode != 0:
			raise RTCError('failed to execute RTC CLI command "%s" (status: %s):\n%s' % (cmd, p.returncode, e))
		log.info('back from RTC CLI command.')
		return o
	def compare_baselines(self, component, b1, b2):
		return self.execute('compare -r "%s" --component "%s" baseline "%s" baseline "%s"'
							% (self.server.url, component, b1, b2), scm_opts='-a n -u y')
	def compare_baseline_tip(self, component, b1, s1):
		return self.execute('compare -r "%s" --component "%s" baseline "%s" stream "%s"'
							% (self.server.url, component, b1, s1), scm_opts='-a n -u y')
	def migrate_baseline(self, baseline, work_item):
		txt = self.execute('checkin --delim-none .', scm_opts='-a n -u y')
		log.debug(txt)
		m = re.compile(r'(?m)Change sets:\W*\(([-_A-Za-z0-9]+)\)').search(txt)
		if m:
			csid = m.group(1)
		else:
			raise RTCError('unable to find changeset id in: \n%s' % txt)

		# set changeset comment to CCM task id.
		log.info(self.execute('changeset comment "%s" "migrating baseline \'%s\'"' % (csid, baseline)))

		# associate changeset to common work item.
		log.info(self.execute('changeset associate "%s" "%s"' % (csid, work_item)))
	def create_snapshot(self, baseline, workspace, stream):
		'create snapshot from sandbox (current working directory)'
		log.info('creating RTC snapshot "%s"' % baseline)

		ss_txt = self.execute("create snapshot -n '%s' '%s'" % (baseline, workspace), scm_opts='-a n -u y')
		log.debug(ss_txt)
		m = re.compile(r'(?ms)Snapshot \(([-_A-Za-z0-9]+)\) .* successfully created').search(ss_txt)
		if m:
			ssid = m.group(1)
			log.info(self.execute("deliver"))
			log.info(self.execute("snapshot promote '%s' '%s'" % (stream, ssid)))
		else:
			raise RTCError('unable to create snapshot "%s" in workspace "%s"' % (baseline, workspace))

class RTC(object):
	'''
	An RTC instance
	'''
	path_auth_id = 'jts/authenticated/identity'
	path_auth_check = 'jts/authenticated/j_security_check'
	def __init__(self, host = None, root='ccm', user=None, password=None):
		self.server = Server(host=host, root=root)
		self.authenticated = False
		self.user = user
		self.password = password
		self._last_response = ''
		self._auth_timeout = 3600		# reauthenticate every hour (3600 seconds)
		tmpdir = os.getenv('TMPDIR')
		tmpdir = tmpdir if (tmpdir and os.path.isdir(tmpdir)) else '/tmp'
		self.cookie_file = ('%s/cookie.%s' % (tmpdir, self.server.host))
	def remove_cookie_file(self):
		try:
			os.remove(self.cookie_file)
		except OSError:
			pass
	def reauthenticate(self):
		self.authenticated = False
		self.authenticate()
	def authenticate(self):
		if self.authenticated:
			if (time.time() - self._authtime) > self._auth_timeout:
				# time to reauthenticate
				log.info('60 minutes since last authentication, reauthenticating now')
				self.authenticated = False
			else:
				# existing authentication should still be valid
				return
		log.info("Authenticating for REST...")
		self.remove_cookie_file()
		try:
			self.create_session_id()
			self.check_auth()
		except:
			raise RTCError('unable to authenticate')
		self.authenticated = True
		self._authtime = time.time()
		log.info("Authenticated for REST.")
	def create_session_id(self):
		opts = {pycurl.COOKIEJAR:      self.cookie_file}
		self.do_curl(opts, '%s/%s' % (self.server.url, self.path_auth_id))
	def check_auth(self):
		postfields = [('j_username', self.user),
					  ('j_password', self.password),]
		opts = {pycurl.POSTFIELDS:     urllib.urlencode(postfields),
				pycurl.COOKIEJAR:      self.cookie_file}
		response = self.do_curl(opts, '%s/%s' % (self.server.url, self.path_auth_check))
		if (('authrequired' in response)
			or
			('authfailed' in response)):
			raise RTCError('Unable to authenticate "%s"' % self.user)
	@retry(RTCError, tries=9, delay=8, backoff=2, logger=log)
	def do_curl(self, options, url):
		def header(buf):
			self._body_offset += len(buf)
		def capture(buf):
			self._last_response += buf
		self._body_offset = 0
		self._last_response = ''
		all_opts = {pycurl.URL:            str(url),
					pycurl.VERBOSE:        0,
					pycurl.SSL_VERIFYHOST: 0,
					pycurl.SSL_VERIFYPEER: 0,
					pycurl.FOLLOWLOCATION: 1,
					pycurl.COOKIEFILE:     self.cookie_file,
					pycurl.WRITEFUNCTION:  capture,
					pycurl.HEADERFUNCTION: header}
		if options:
			all_opts.update(options)
		curl = pycurl.Curl()
		for k, v in all_opts.items():
			curl.setopt(k, v)
		try:
			curl.perform()
			log.info(curl.getinfo(curl.EFFECTIVE_URL))
		except pycurl.error, v:
			rcode = curl.getinfo(pycurl.RESPONSE_CODE)
			if int(rcode) == 302:
				'''According to https://jazz.net/library/article/194, HTTP response code 302 means
				   we have an authentication failure and server is redirecting us to login UI.
				   This can happen even if we have previously authenticated, but for some reason,
				   like CCM delays, need to reauthenticate.
				   Try to reauthenticate, then raise RTCError, and hope that retry logic works.
				   '''
				log.info('do_curl received HTTP response code 302, reauthenticating...')
				self.reauthenticate()
			raise RTCError('unable to perform CURL operation (RTC response code: %s)'
						   % rcode)
		finally:
			curl.close()
		return self._last_response
	def rest(self, path, data=None, options=None, headers=None):
		self.authenticate()
		url = '%s/%s' % (self.server.url, path)
		self._last_url = url
		log.info('making rest call to "%s"' % url)
		log.info('cookie file is "%s"' % self.cookie_file)
		all_headers = ['Accept: application/x-oslc-cm-changerequest+json',
					   'Content-Type: application/x-oslc-cm-changerequest+json']
		if headers:
			all_headers += headers
		opts = {pycurl.HEADER:          1,
				pycurl.HTTPHEADER:      all_headers}
		if options:
			opts.update(options)
		r = self.do_curl(opts, url)
		return (r[:self._body_offset], r[self._body_offset:])
	def discover(self, target_project_name):
		'''
		Discover important things about specified project
		'''
		self.authenticate()
		try:
			return self._discovery
		except:
			pass

		root_svcs_url = '%s/%s' % (self.server.url, 'rootservices')
		root_xml = self.do_curl(None, root_svcs_url)

		# get root discovery document
		root_disc_doc = minidom.parseString(root_xml)

		# look up service provider catalog
		service_providers = root_disc_doc.getElementsByTagName('oslc_cm:cmServiceProviders')[-1]
		catalog_url = service_providers.getAttribute('rdf:resource')
		catalog = minidom.parseString(self.do_curl(None, catalog_url))

		# get list of service providers
		service_providers = catalog.getElementsByTagName('oslc_disc:ServiceProvider')

		# find service provider URL for target project
		try:
			target_project_el = [sp
								 for sp in service_providers
				                 if (str(sp.getElementsByTagName('dc:title')[-1].lastChild.data)
									 == target_project_name)][-1]
		except IndexError:
			raise RTCError('project "%s" not found.' % target_project_name)
		services_url = target_project_el.getElementsByTagName('oslc_disc:services')[-1].getAttribute('rdf:resource')

		# get service descriptor
		service_descriptor = minidom.parseString(self.do_curl(None, services_url))

		# find default work item factory url
		factories = service_descriptor.getElementsByTagName('oslc_cm:factory')
		factory_el = [f for f in factories if f.getAttribute('oslc_cm:default') == u'true'][0]
		factory_url = factory_el.getElementsByTagName('oslc_cm:url')[-1].firstChild.data

		# find default work item query url
		query_el = service_descriptor.getElementsByTagName('oslc_cm:simpleQuery')[0]
		query_url = query_el.getElementsByTagName('oslc_cm:url')[-1].firstChild.data

		# extract project's UUID
		m = re.compile(r'%s/(.*)' % self.server.url).match(factory_url)
		factory_path = m.group(1)
		m = re.compile(r'oslc/contexts/(.*)/workitems').match(factory_path)
		project_uuid = m.group(1)

		self._discovery = { 'WorkItemFactory': factory_path,
							'ProjectUUID':     project_uuid,
							'Query':           query_url,
							'Raw':             service_descriptor.toprettyxml(),
							'CookieFile':      self.cookie_file}
		return self._discovery

class WorkItem(object):
	'''
	RTC Work Item

    Current implementation GETs and PUTs (and POSTs, etc) JSON. XML
    would be better, but the current environment rejects XML
    PUTs. Until this is corrected, this tool will use JSON, with the
    expectation that JSON representation will be replaced by XML
    eventually.

    Internalized data is stored in self._data.
	'''
	id_re = re.compile(r'^([0-9]+)$')
	def __init__(self, rtc, idOrProject):
		'''
		initialize a new WorkItem object

		idOrProject is either the id of an existing work item, or
		the name of an existing project. If it is a project name,
		then a new work item will be created.
		'''
		self._rtc = rtc
		self._data = None
		self._etag = None
		self._project = None
		if WorkItem.id_re.match(str(idOrProject)):
			self.id = idOrProject
		else:
			self.id = self._create(idOrProject)
	def _extract_etag(self, headers):
		etag_re = re.compile(r'ETag: "(.*?)"')
		m = etag_re.search(headers)
		if m:
			return m.group(1)
		else:
			log.error('missing or invalid etag in work item %s\n"%s"' % (self.id, headers))
			pass # raise RTCError('missing or invalid etag in work item %s' % self.id)
	template = '''{
	"dc:title":"%(title)s",
	"dc:description":"%(description)s",
	"rtc_cm:cdets": "build", 
	"dc:type":
	  {
		"rdf:resource":"%(type)s"
	  }
	}'''
	def _create(self, project):
		pd = self._rtc.discover(project)
		self._project = pd['ProjectUUID']
		m = re.compile(r'(.*)/contexts/(%s)/workitems' % pd['ProjectUUID']).match(pd['WorkItemFactory'])
		t = '%s/%s/types/%s/task' % (self._rtc.server.url, m.group(1), pd['ProjectUUID'])
		_json = self.template % {'title': 'template-generated work item',
								   'description': 'a new work item generated from template created by rtc.py.',
								   'type': t}
		buf = StringIO.StringIO(_json)
		options = { pycurl.READFUNCTION: FileReader(buf).read_callback,
					pycurl.POSTFIELDSIZE: len(_json),
					pycurl.POST: 1 }
		self._headers, _json = self._rtc.rest(path=pd['WorkItemFactory'], options=options)
		self._data = json.loads(_json)
		self._etag = self._extract_etag(self._headers)
		try:
			return self._data['dc:identifier']
		except KeyError:
			errinfo = { "json": _json,
					    "data": pformat(self._data) }
			raise RTCError('"dc:identifier" missing in response from work item factory:\n%s"'
						   % pformat(errinfo))
	def flush(self):
		_json = json.dumps(self._data)
		buf = StringIO.StringIO(_json)
		buflen = len(_json)
		options = {pycurl.READFUNCTION: FileReader(buf).read_callback,
				   pycurl.INFILESIZE: len(_json),
				   pycurl.PUT: 1}
		headers = ['If-Match: %s' % self._etag]
		self._headers, _json = self._rtc.rest(path=('oslc/workitems/%s' % self.id),
											  options=options, headers=headers)
		self._data = json.loads(_json)
		self._etag = self._extract_etag(self._headers)
		try:
			return self._data['dc:identifier']
		except KeyError:
			errinfo = { "json": _json,
					    "data": pformat(self._data) }
			raise RTCError('"dc:identifier" missing in response to work item update:\n%s"'
						   % pformat(errinfo))
	def _get_data(self):
		'fetch ".../ccm/oslc/workitems/%s" % self.id'
		if self._data:
			return
		self._headers, _json = self._rtc.rest(path='oslc/workitems/%s' % self.id)
		try:
			self._data = json.loads(_json)
			self._etag = self._extract_etag(self._headers)
		except:
			raise RTCError('failure in work item retrieval or parsing:\njson:%s\nheaders:%s' % (_json, self._headers))
	def etag(self):
		self._get_data()
		return self._etag
	def getset(self, name, value=None):
		'''
		Get, and optionally set,  work item attribute

		A more elegant solution would be override __getattr__ and __setattr__. This is
		not easy, because attribute names use XML namespace syntax. The embedded ':'
		results in invalid syntax, e.g. wi.rtc_cm:cdets ("rtc_cm:cdets" is not a valid
		attribute reference). For now, use getset and attribute-specific
		functions like self.cdets and self.state.
		'''
		assert self.id is not None
		self._get_data()
		prev = self._data[name]
		if value:
			self._data[name] = value
		return prev
	def state(self, value=None):
		num_2_str = {'1': 'New',
					 '2': 'In Progress',
					 '3': 'Done'}
		state_uri = self.getset('rtc_cm:state')['rdf:resource']
		r = state_uri[-1]
		if value:
			state_uri = state_uri[0:-1] + value
			self._data['rtc_cm:state']['rdf:resource'] = state_uri
		return r
	def cdets(self, value=None):
		return self.getset('rtc_cm:cdets', value)
	def title(self, value=None):
		return self.getset('dc:title', value)
	def changesets(self, value=None):
		return self.getset('rtc_cm:com.ibm.team.filesystem.workitems.change_set.com.ibm.team.scm.ChangeSet', value)

def main():
	import logging as log
	log.basicConfig(level=log.DEBUG, format='%(asctime)s %(message)s')
	try:
		user, password = sys.argv[1], sys.argv[2]
	except IndexError:
		raise RTCError('usage: %s <user> <password>' % sys.argv[0])
	# rtc = RTC(host='cornet-ccm1.cisco.com', root='ccm', user=user, password=password)
	rtc = RTC(host='rtp-scmrtc-ccm1.cisco.com', root='ccm1', user=user, password=password)
	cli = CLI(rtc.server, user, password)
	if True:
		# test reauthentication
		rtc.authenticate()
		rtc.discover('NGP-Diag')
		id = sys.argv[3]

		log.info('fetching WI %s' % id)
		wi = WorkItem(rtc, id)

		print id, wi.title(), wi.cdets(), wi.state()
		wi.state('3')
		print id, wi.title(), wi.cdets(), wi.state()

		log.info('subtracting 3601 to RTC auth time to force re-auth')
		rtc._authtime -= 3601

		log.info('fetching WI %s' % id)
		wi = WorkItem(rtc, id)
		print id, wi.title(), wi.cdets(), wi.state()

		# pprint(wi._data)
		print(json.dumps(wi._data, indent=2))
	else:
		# try creating a new work item:
		print '\n\nnew work item:'
		nwi = WorkItem(rtc, "MW_Axiom")
		print(json.dumps(nwi._data, indent=2))

if __name__ == "__main__":
	main()
