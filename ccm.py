'''
CM/Synergy
'''
import os, os.path, re, StringIO, subprocess, sys
import pdb
import logging as log
from pprint import *
from datetime import datetime as dt
from retry import retry

class CCMError(Exception):
	pass

def remove_dcm_prefix(name):
	m = re.compile(r'(\w+=)?(.*)').match(name)
	return m.group(2) if m else ''

class CCM(object):
	def __init__(self, server=('sausatlccmdb1', '/data/ccmdb/atl_client_db'), ccm='ccm'):
		self.server, self.ccm = server, ccm
		self.ccm_addr = os.getenv('CCM_ADDR')
		if not self.ccm_addr:
			self.ccm_addr = self.execute("start -q -m -rc -nogui -h '%s' -d '%s'"
										 % (self.server[0], self.server[1]))
			os.putenv('CCM_ADDR', self.ccm_addr)
			log.debug('CCM session started, CCM_ADDR=%s' % self.ccm_addr)
		else:
			log.debug('using existing CCM session, CCM_ADDR=%s' % self.ccm_addr)
	@retry(CCMError, tries=9, delay=8, backoff=2, logger=log)
	def execute(self, cmd, ccm_opts='', ignore_out = None, ignore_err = None):
		'execute ccm command line, ignoring certain errors that match ignore_out or ignore_err patterns'
		cl = '%s %s %s' % (self.ccm, ccm_opts, cmd)
		log.info('starting CCM CLI command: %s' % cl)
		p = subprocess.Popen(cl, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		o, e = p.communicate()
		if p.returncode != 0:
			# special-case handling for certain CCM errors...
			if ignore_err and re.compile(ignore_err).search(e):
				'standard error pattern matches, ignore this error'
				log.error('error in CCM CLI command "%s", ignoring:\nstandard output: <<%s>>\nstandard error: <<%s>>"'
						  % (cmd, o, e))
				return e
			elif ignore_out and re.compile(ignore_out).search(o):
				'standard output pattern matches, ignore this error'
				log.error('error in CCM CLI command "%s", ignoring:\nstandard output: <<%s>>\nstandard error: <<%s>>"'
						  % (cmd, o, e))
				return o
			else:
				# CCM does not consistently show errors on standard error, sometimes it
				# shows errors on standard output, so show both...
				raise CCMError('failed to execute CCM CLI command "%s":\nstandard output: <<%s>>\nstandard error: <<%s>>"'
							   % (cmd, o, e))
		log.info('back from CCM CLI command.')
		return o
	def execute_failok(self, cmd, ccm_opts=''):
		cl = '%s %s %s' % (self.ccm, ccm_opts, cmd)
		log.info('starting CCM CLI command: %s' % cl)
		p = subprocess.Popen(cl, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		o, e = p.communicate()
		if p.returncode != 0:
			log.error('failed to execute CCM CLI command "%s" (ignoring):\nstandard output: <<%s>>\nstandard error: <<%s>>"'
					  % (cmd, o, e))
			return None
		log.info('back from CCM CLI command.')
		return o
	def text2list(self, text):
		return [l.rstrip() for l in text.split('\n') if l]
	def baseline_compare(self, bl1, bl2, pjt_name):
		'''
		return (a, r) where, in order to get a working project at bl1 to bl2,
		  a is the list of tasks to be added (in bl2, but not in bl1)
		  r is the list of tasks to be removed (in bl1, but not in bl2)

		  format of a and r is:
		  ['cup=25637', 'cup=26080', ...]
		'''
		def text2tasks(text):
			tasks = set()
			# split text at '\n' then split lines at ',' to get list of task ids;
			for v in [u.split(',') for u in [t.strip() for t in text.split('\n') if t]]:
				tasks.update(v)
			return tasks
		cmd_tmpl = '''query "recursive_is_member_of('%s','none')" -u -ns -f "%%task"'''
		blp1 = self.baseline_project(bl1, pjt_name)
		blp2 = self.baseline_project(bl2, pjt_name)
		tasks_in_bl1 = text2tasks(self.execute(cmd_tmpl % blp1))
		tasks_in_bl2 = text2tasks(self.execute(cmd_tmpl % blp2))
		tasks2add = tasks_in_bl2 - tasks_in_bl1
		tasks2remove = tasks_in_bl1 - tasks_in_bl2
		return (tasks2add, tasks2remove)
	def baseline_compare_x(self, bl1, bl2):
		'''
		another flavor of baseline_compare that uses "ccm baseline -compare"
		return (a, r) where, in order to get a working project at bl1 to bl2,
		  a is the list of tasks to be added (in bl2, but not in bl1)
		  r is the list of tasks to be removed (in bl1, but not in bl2)

		  format of a and r is:
		  ['cup=25637',
		   'cup=26080',
		   'cup=26087',
		   ...
		  ]
		'''
		pt = self.execute("baseline -compare '%s' '%s' -tasks" % (bl1, bl2))
		bl1_re = re.compile('Tasks only in Baseline %s' % bl1)
		bl2_re = re.compile('Tasks only in Baseline %s' % bl2)
		both_re = re.compile('Tasks in both Baseline')
		task_re = re.compile(r'(\w+=\d+) (\w*)')
		in_bl1 = False
		in_bl2 = False
		bl1_tasks = list()
		bl2_tasks = list()
		for l in pt.split('\n'):
			m = bl1_re.match(l)
			if m:
				in_bl1 = True
				in_bl2 = False
				continue
			m = bl2_re.match(l)
			if m:
				in_bl1 = False
				in_bl2 = True
				continue
			m = both_re.match(l)
			if m:
				in_bl1 = False
				in_bl2 = False
				continue
			m = task_re.match(l)
			if in_bl1 and m:
				bl1_tasks.append(m.group(1))
			if in_bl2 and m:
				bl2_tasks.append(m.group(1))
		return (bl2_tasks, bl1_tasks)
	def project_grouping(self, project):
		pg_re = re.compile(r'(?L)Project Grouping (.*)$')
		txt = self.execute("finduse -mpg -p '%s'" % project)
		m = pg_re.search(txt)
		return m.group(1) if m else ''
	def baseline_project(self, baseline, pjt_name):
		'return baseline project_spec given baseline (project name no longer used)'
		bl_projs = [p.strip()
					for p in self.execute('''baseline -show projects "%s" -u -f '%%displayname' '''
									 % baseline).split('\n')
					if p]
		for proj in bl_projs:
			hier = [p.strip()
					for p in self.execute('''query "hierarchy_project_members('%s', none)" -u -f '%%displayname' '''
									 % proj).split('\n')
					if p]
			if len(hier) == len(bl_projs):
				return proj
		if len(bl_projs) == 1:
			return bl_projs[0]
		return None

class Project(object):
	def __init__(self, spec, ccm):
		self._spec = spec
		self._ccm = ccm
		self._init_common()
	def _init_common(self):
		r = self._ccm.execute("info -p '%s' -f '%%name|%%version|%%baseline|%%release'" % self._spec).strip().split('|')
		self.name, self.version, self.baseline_project, self.release = r
	def baselines(self, purposes=None, raw=None):
		'return list of baselines in release.'
		t0 = dt(1970, 1, 1)
		baselines = list()
		if not raw:
			using_bl_cmd = ("baseline -list -release '%s' -purpose 'System Testing' -ns -u -f '%%displayname|%%create_time'"
							% self.release)
			using_query_cmd = '''query -t baseline "release='%s' and (%s)" -ns -u -f '%%displayname|%%create_time' ''' % (self.release, " or ".join(["has_purpose('%s')" % p for p in purposes]))
			raw = self._ccm.execute(using_query_cmd).split('\n')
		else:
			raw = raw.split('\n')
		for bl in raw:
			x = bl.split('|')
			try:
				nm, t1 = x[0], dt.strptime(x[1].strip(), '%c')
				assert t1 > t0
				t1 = t0
				baselines.append(nm)
			except IndexError:
				log.error('invalid line in baseline list (ignoring): "%s"' % str(x))
		return baselines
	def baseline_align(self, baseline):
		log.info('aligning CCM project with baseline "%s"' % baseline)
		bl_proj = self._ccm.baseline_project(baseline, None)
		log.info('baseline project for "%s" is "%s"' % (baseline, bl_proj))
		self.remove_all_tasks()
		self._ccm.execute("update_properties -recurse -modify_baseline_project '%s' '%s'" % (bl_proj, self._spec))
		self.update()
		self._init_common()
	def remove_tasks(self, tasks):
		if not tasks:
			log.info('no tasks to remove from "%s"' % self.baseline_project)
		else:
			try:
				r = self._ccm.execute("update_properties -recurse -remove -tasks '%s' '%s'"
									  % (','.join(tasks), self._spec), ignore_err=r'(?ms)not modifiable by you')
				log.debug(r)
			except CCMError, s:
				log.debug('one or more of the following tasks could not be removed:\n%s\n<<%s>>'
						  % (pformat(tasks), s))
			self.update()
	def update(self):
		r = self._ccm.execute("update -r -p '%s'" % self._spec)
		log.debug(r)
		return r
	def remove_all_tasks(self):
		raw = self._ccm.execute("update_properties -recurse -show tasks -u '%s'" % self._spec).split('\n')
		tasks2remove = list()
		for l in raw:
			try:
				m = re.compile(r'Task ([^:]+):').match(l)
				tasks2remove.append(m.group(1))
			except AttributeError:
				log.debug('no task_spec in: "%s"' % l)
		self.remove_tasks(tasks2remove)

def test():
	import logging as log
	log.basicConfig(level=log.DEBUG, format='%(asctime)s %(message)s')
	ccm = CCM(server=('spvtgccm5', '/data/ccmdb/atl_mw_db'))
	bl = 'cupmw=Axiom_3.1.0.2001'
	bl_proj = ccm.baseline_project(bl, None)
	print bl, bl_proj

if __name__ == '__main__':
	test()
