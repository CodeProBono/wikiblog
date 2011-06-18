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
      #user_prefs = models.UserPrefs(user=users.get_current_user(),name=form.name)
      user_prefs.user = users.get_current_user()
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
  draft = forms.BooleanField(required=False)
  locked = forms.BooleanField(required=False)
  class Meta:
    model = models.BlogPost
    fields = [ 'title', 'body', 'tags', 'locked' ]

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
          'draft': post and not post.path,
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
                    initial={'draft': post and post.published is None})
    if form.is_valid():
      post = form.save(commit=False)
      if form.clean_data['draft']:# Draft post
        post.published = datetime.datetime.max
        post.put()
      else:
        if not post.path: # Publish post
          post.updated = post.published = datetime.datetime.now()
          post.original_author_as_user = users.get_current_user() # Only assign the original user on first save of non-draft post.
          # Find this user's name string
          q = db.GqlQuery("SELECT * FROM UserPrefs WHERE user = :1", post.original_author_as_user)
          userprefs = q.get()
          if userprefs:
            post.original_author_name = userprefs.name # Set user name string for this post.
          logging.info('PostHandler.post in handlers.py, original_author_name = ' + str(post.original_author_name))
        else:# Edit post
          post.updated = datetime.datetime.now()
          # Find this user's name string
          q = db.GqlQuery("SELECT * FROM UserPrefs WHERE user = :1", users.get_current_user())
          userprefs = q.get()
          # Add additional authors to editors list, provided they aren't the
          # original author, and aren't already in the list.
          logging.info('PostHandler.post in handlers.py, editors started = ' + str(post.editors))
          if userprefs and userprefs.name != post.original_author_name:
            if userprefs.name not in post.editors:
              post.editors.append( userprefs.name )
            post.locked = False # Only the original author or an admin should
            # be able to lock posts.
            # TODO: the form should make this clear by greying-out the
            # button or something similar.
          logging.info('PostHandler.post in handlers.py, editors finished = ' + str(post.editors))
          
        post.put()
        post.publish()
      self.render_to_response("published.html", {
          'post': post,
          'draft': form.clean_data['draft']})
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
