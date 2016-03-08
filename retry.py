import time

def retry(ExceptionToCheck, tries=4, delay=3, backoff=2, logger=None):
	'''
	decorator to wrap function with retry logic

	tries:   number of attempts to make before giving up
	delay:   initial delay between attempts
	backoff: multiplier to increase delay
	'''
	def deco_retry(f):
		def f_retry(*args, **kwargs):
			mtries, mdelay = tries, delay
			try_one_last_time = True
			while mtries > 1:
				try:
					return f(*args, **kwargs)
					try_one_last_time = False
					break
				except ExceptionToCheck, e:
					msg = "%s, Retrying in %d seconds..." % (str(e), mdelay)
					if logger:
						logger.warning(msg)
					else:
						print msg
					time.sleep(mdelay)
					mtries -= 1
					mdelay *= backoff
			if try_one_last_time:
				return f(*args, **kwargs)
			return
		return f_retry
	return deco_retry
