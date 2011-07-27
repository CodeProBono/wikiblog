from google.appengine.ext import webapp
from google.appengine.ext.webapp import template
from google.appengine.ext.webapp.util import run_wsgi_app
from google.appengine.api import users
from google.appengine.ext import db

import fix_path
import models
import utils

from django import newforms as forms
from google.appengine.ext.db import djangoforms

# Extra handler for setting user profile details on first login.
class UserProfileForm(djangoforms.ModelForm):
    name = forms.CharField(widget=forms.TextInput(attrs={'id':'name'}))
    class Meta:
        model = models.UserPrefs
        fields = [ 'name' ]
        
class UserProfileHandler(webapp.RequestHandler):
    def render_form(self, form):
        """ accepts a form, and renders a page containing it. """
        self.response.out.write(utils.render_template('userprofile.html', {'form': form}, None))

    def get(self):
        """ generates the page with a blank form when it's requested
        by the user's browser with a GET request."""
        self.render_form(UserProfileForm(data=self.request.POST))

    def post(self):
        """ accepts form submissions and checks them for validity.
        If the form isn't valid, it shows the user the submission form again;
        Django takes care of including error messages and filling out values
        that the user already entered. If the form is valid, it sets the users
        preferences in a UserPrefs object """
        form = UserProfileForm(data=self.request.POST)
        if form.is_valid():
            user_prefs = form.save(commit=False)
            user_prefs.user = users.get_current_user()
            user_prefs.put()
            self.redirect('/')
        else:
            self.render_form(form)

application = webapp.WSGIApplication([
                                ('/userprofile', UserProfileHandler),                            
                            ])


def main():
    fix_path.fix_sys_path()
    run_wsgi_app(application)


if __name__ == '__main__':
    main()
