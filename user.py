from google.appengine.ext import webapp
from google.appengine.ext.webapp import template
from google.appengine.ext.webapp.util import run_wsgi_app
from google.appengine.api import users
from google.appengine.ext import db

import fix_path
import models
import utils
import user_handlers
import config

from django import newforms as forms
from google.appengine.ext.db import djangoforms

application = webapp.WSGIApplication([
                                (config.url_prefix + '/user', user_handlers.UserProfileHandler),
                                (config.url_prefix + '/user/newpost', user_handlers.PostHandler), # Write a new post.
                                (config.url_prefix + '/user/post/(\d+)', user_handlers.PostHandler), # Add or edit a post given its key
                                (config.url_prefix + '/user/post/preview/(\d+)', user_handlers.PreviewHandler)
                            ])

def main():
    fix_path.fix_sys_path()
    run_wsgi_app(application)


if __name__ == '__main__':
    main()
