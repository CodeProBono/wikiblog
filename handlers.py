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
  class Meta: # MF: Where/how is this used?
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
    # User must be an admin to reach this handler.
    loginout_url = users.create_logout_url(self.request.uri)
    url_linktext = 'Logout'
    q = db.GqlQuery('SELECT * FROM UserPrefs WHERE user = :1', users.get_current_user())
    userprefs = q.get()
    user_name = users.get_current_user().nickname() # Default to email if we can't find the user prefs, but this shouldn't actually happen...
    if userprefs:
        user_name = userprefs.name

    template_vals.update({
        'path': self.request.path,
        'handler_class': self.__class__.__name__,
        'is_admin': True,
        'loginout_url': loginout_url,
        'user_name': user_name,
        'url_linktext': url_linktext,
    })
    template_name = os.path.join('admin', template_name)
    self.response.out.write(utils.render_template(template_name, template_vals,
                                                  theme))


class AdminHandler(BaseHandler):
  def get(self):
    """ Lists start to count worth of posts. """
    offset = int(self.request.get('start', 0))
    count = int(self.request.get('count', 20))
    posts = models.BlogPost.all().order('-published').fetch(count, offset)
    template_vals = {
        'offset': offset,
        'count': count,
        'last_post': offset + len(posts) - 1,
        'prev_offset': max(0, offset - count),
        'next_offset': offset + count,
        'posts': posts,
    }
    self.render_to_response('index.html', template_vals)


class PostHandler(BaseHandler):
  def render_form(self, form):
    """ accepts a form, and uses render_to_response to render a page containing the form. """
    self.render_to_response('edit.html', {'form': form})

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
          q = db.GqlQuery('SELECT * FROM UserPrefs WHERE user = :1', post.original_author_as_user)
          userprefs = q.get()
          if userprefs:
            post.original_author_name = userprefs.name # Set user name string for this post.
          logging.info('PostHandler.post in handlers.py, original_author_name = ' + str(post.original_author_name))
        else:# Edit post
          post.updated = datetime.datetime.now()
          # Find this user's name string
          q = db.GqlQuery('SELECT * FROM UserPrefs WHERE user = :1', users.get_current_user())
          userprefs = q.get()
          # Add additional authors to editors list, provided they aren't the
          # original author, and aren't already in the list.
          logging.info('PostHandler.post in handlers.py, editors started = %s' % str(post.editors))
          if userprefs and userprefs.name != post.original_author_name \
                       and userprefs.name not in post.editors:
            post.editors.append( userprefs.name )
          logging.info('PostHandler.post in handlers.py, editors finished = %s' % str(post.editors))
        post.put()
        post.publish()
        logging.info('PostHandler.post in handlers.py, post.path = ' + str(post.path))
      self.render_to_response("published.html", {
        'post': post,
        'draft': form.clean_data['draft']})
    else:
      self.render_form(form)

class DeleteHandler(BaseHandler):
  @with_post
  def post(self, post):
    if post.path:# Published post
      post.remove()
    else:# Draft
      post.delete()
    self.render_to_response('deleted.html', None)


class PreviewHandler(BaseHandler):
  @with_post
  def get(self, post):
    # Temporarily set a published date iff it's still
    # datetime.max. Django's date filter has a problem with
    # datetime.max and a "real" date looks better.
    if post.published == datetime.datetime.max:
      post.published = datetime.datetime.now()
    self.response.out.write(utils.render_template('post.html', {
        'post': post,
        'is_admin': True}))


class RegenerateHandler(BaseHandler):
  def post(self):
    deferred.defer(post_deploy.TagCloudRegenerator().regenerate) # Added by Tom.
    deferred.defer(post_deploy.PostRegenerator().regenerate)
    deferred.defer(post_deploy.PageRegenerator().regenerate)
    deferred.defer(post_deploy.try_post_deploy, force=True)
    self.render_to_response('regenerating.html')


class PageForm(djangoforms.ModelForm):
  path = forms.RegexField(
    widget=forms.TextInput(attrs={'id':'path'}), 
    regex='(/[a-zA-Z0-9/]+)')
  title = forms.CharField(widget=forms.TextInput(attrs={'id':'title'}))
  template = forms.ChoiceField(choices=config.page_templates.items())
  body = forms.CharField(widget=forms.Textarea(attrs={
      'id':'body',
      'rows': 10,
      'cols': 20}))
  class Meta:
    model = models.Page
    fields = [ 'path', 'title', 'template', 'body' ]

  def clean_path(self):
    data = self._cleaned_data()['path']
    existing_page = models.Page.get_by_key_name(data)
    if not data and existing_page:
      raise forms.ValidationError('The given path already exists.')
    return data


class PageAdminHandler(BaseHandler):
  def get(self):
    offset = int(self.request.get('start', 0))
    count = int(self.request.get('count', 20))
    pages = models.Page.all().order('-updated').fetch(count, offset)
    template_vals = {
        'offset': offset,
        'count': count,
        'prev_offset': max(0, offset - count),
        'next_offset': offset + count,
        'last_page': offset + len(pages) - 1,
        'pages': pages,
    }
    self.render_to_response("indexpage.html", template_vals)


def with_page(fun):
  def decorate(self, page_key=None):
    page = None
    if page_key:
      page = models.Page.get_by_key_name(page_key)
      if not page:
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.out.write('404 :(\n%s' % page_key)
        return
    fun(self, page)
  return decorate


class PageHandler(BaseHandler):
  def render_form(self, form):
    self.render_to_response('editpage.html', {'form': form})

  @with_page
  def get(self, page):
    self.render_form(PageForm(
        instance=page,
        initial={
          'path': page and page.path or '/',
        }))

  @with_page
  def post(self, page):
    form = None
    # if the path has been changed, create a new page
    if page and page.path != self.request.POST['path']:
      form = PageForm(data=self.request.POST, instance=None, initial={})
    else:
      form = PageForm(data=self.request.POST, instance=page, initial={})
    if form.is_valid():
      oldpath = form._cleaned_data()['path']
      if page:
        oldpath = page.path
      page = form.save(commit=False)
      page.updated = datetime.datetime.now()
      page.publish()
      # path edited, remove old stuff
      if page.path != oldpath:
        oldpage = models.Page.get_by_key_name(oldpath)
        oldpage.remove()
      self.render_to_response('publishedpage.html', {'page': page})
    else:
      self.render_form(form)


class PageDeleteHandler(BaseHandler):
  @with_page
  def post(self, page):
    page.remove()
    self.render_to_response('deletedpage.html', None)
