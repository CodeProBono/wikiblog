import datetime
import logging
import os

from google.appengine.ext import deferred
from google.appengine.ext import webapp

import config
import markup
import models
import post_deploy
import utils
import logging

from django import newforms as forms
from google.appengine.ext.db import djangoforms
from google.appengine.api import users

# Extra handler for setting user profile details on first login.
class UserProfileForm(djangoforms.ModelForm):
  name = forms.CharField(widget=forms.TextInput(attrs={'id':'name'}))
  class Meta:
    model = models.UserPrefs
    fields = [ 'name' ]
    
class UserProfileHandler(webapp.RequestHandler):
  def render_form(self, form):
    """ accepts a form, and renders a page containing it. """
    self.response.out.write(utils.render_template("userprofile.html", {'form': form}, None))

  def get(self):
    """ generates the page with a blank form when it's requested
    by the user's browser with a GET request."""
    # If there's a current user logged in, pre-fill the form with
    # this user's details.
    from google.appengine.ext import db
    q = db.GqlQuery("SELECT * FROM UserPrefs WHERE user = :1", users.get_current_user())
    if q.get():
      initial_data = {'name': q.get().name}
      logging.info('UserProfileHandler.get in user_handlers.py, initial_data = ' + str(initial_data))
      self.render_form(UserProfileForm(instance=q.get(),initial=initial_data))
    else:
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
      user_prefs.publish() # Publish finds a unique URL path for this user's author page.
      user_prefs.put()
      self.redirect("/")
    else:
      self.render_form(form)

class PostForm(djangoforms.ModelForm):
  title = forms.CharField(widget=forms.TextInput(attrs={'id':'name'}))
  body = forms.CharField(widget=forms.Textarea(attrs={
      'id':'message',
      'rows': 10,
      'cols': 20}))
  body_markup = forms.ChoiceField(
    choices=[(k, v[0]) for k, v in markup.MARKUP_MAP.iteritems()])
  tags = forms.CharField(widget=forms.Textarea(attrs={'rows': 5, 'cols': 20}))
  locked = forms.BooleanField(required=False)
  anonymous = forms.BooleanField(required=False)
  class Meta:
    model = models.BlogPost
    fields = [ 'title', 'body', 'tags', 'locked', 'anonymous' ]

def with_post(fun):
  """ Decorator function that attaches to methods that require an optional
  post ID. Loads the relevant BlogPost object. """
  def decorate(self, post_id=None):
    post = None
    if post_id:
      post = models.BlogPost.get_by_id(int(post_id))
      if not post:
        self.error(404)
        return
    fun(self, post)
  return decorate

class BaseHandler(webapp.RequestHandler):
  def render_to_response(self, template_name, template_vals=None, theme=None):
    from google.appengine.ext import db
    if not template_vals:
      template_vals = {}
    # User must be logged in to reach this handler.
    loginout_url = users.create_logout_url(self.request.uri)
    url_linktext = 'Logout'
    q = db.GqlQuery("SELECT * FROM UserPrefs WHERE user = :1", users.get_current_user())
    userprefs = q.get()
    user_name = users.get_current_user().nickname() # Default to email if we can't find the user prefs, but this shouldn't actually happen...
    if userprefs:
        user_name = userprefs.name

    template_vals.update({
        'path': self.request.path,
        'handler_class': self.__class__.__name__,
        'is_admin': users.is_current_user_admin(),
        'loginout_url': loginout_url,
        'user_name': user_name,
        'url_linktext': url_linktext,
    })
    self.response.out.write(utils.render_template(template_name, template_vals,
                                                  theme))

class PostHandler(BaseHandler):
  def render_form(self, form):
    """ accepts a form, and uses render_to_response to render a page containing the form. """
    self.render_to_response("edit.html", {'form': form})

  @with_post
  def get(self, post):
    """ generates the page with a blank form when it's requested by the user's browser with a GET request.
    If no post ID is supplied (via the decorator function with_post), post
    is None, and the form works as it used to. If a post ID is supplied,
    the post variable contains the post to be edited, and the form pre-fills
    all the relevant information. The same applies to the post() method.
    """
    self.render_form(PostForm(
        instance=post,
        initial={
          'body_markup': post and post.body_markup or config.default_markup,
        }))

  @with_post
  def post(self, post):
    """ accepts form submissions and checks them for validity.
    If the form isn't valid, it shows the user the submission form again;
    Django takes care of including error messages and filling out values
    that the user already entered. If the form is valid, it saves the form,
    creating a new entity. It then calls .publish() on the new BlogPost
    entity. """
    
    from google.appengine.ext import db
    form = PostForm(data=self.request.POST, instance=post,
                    initial={})
    if form.is_valid():
      post = form.save(commit=False)

      if not post.path: # Publish post
        post.updated = post.published = datetime.datetime.now()
        post.original_author_as_user = users.get_current_user() # Only assign the original user on first save of non-draft post.
        # Find this user's name string
        q = db.GqlQuery("SELECT * FROM UserPrefs WHERE user = :1", post.original_author_as_user)
        userprefs = q.get()
        if userprefs and not form._cleaned_data()['anonymous']: # If user asked to be anonymous, don't record their name.
          post.original_author_name = userprefs.name # Set user name string for this post.
        else:
          post.original_author_name = "Anonymous" # Change original author name to "Anonymous"
        logging.info('PostHandler.post in user_handlers.py, original_author_name = ' + str(post.original_author_name))
      else:# Edit post
        post.updated = datetime.datetime.now()
        # Find this user's name string
        q = db.GqlQuery("SELECT * FROM UserPrefs WHERE user = :1", users.get_current_user())
        userprefs = q.get()
        # Add additional authors to editors list, provided they aren't the
        # original author, and aren't already in the list.
        logging.info('PostHandler.post in user_handlers.py, editors started = ' + str(post.editors))
        if userprefs:
          if userprefs.name != post.original_author_name: # If edited by someone not the original author
            editor_name = userprefs.name
            if form._cleaned_data()['anonymous']: # If user asked to be anonymous
              editor_name = "Anonymous"
              if userprefs.name in post.editors: # If they were previously an editor
                post.editors.remove(userprefs.name) # remove their name.
            if editor_name not in post.editors:
              post.editors.append( editor_name )
            post.locked = False # Only the original author or an admin should
            # be able to lock posts.
            # TODO: the form should make this clear by greying-out the
            # button or something similar.
          else: # If being edited by the original author
            if form._cleaned_data()['anonymous']: # If user asked to be anonymous
              post.original_author_name = "Anonymous" # Change original author name to "Anonymous"
        logging.info('PostHandler.post in user_handlers.py, editors finished = ' + str(post.editors))
      
      post.put()
      post.publish()
      logging.info('PostHandler.post in user_handlers.py, post.path = ' + str(post.path))
      self.render_to_response("published.html", {
          'post': post})
    else:
      self.render_form(form)

class PreviewHandler(BaseHandler):
  @with_post
  def get(self, post):
    # Temporarily set a published date iff it's still
    # datetime.max. Django's date filter has a problem with
    # datetime.max and a "real" date looks better.
    if post.published == datetime.datetime.max:
      post.published = datetime.datetime.now()
    self.response.out.write(utils.render_template('post.html', {
        'post': post}))
