import aetycoon
import datetime
import hashlib
import re
from google.appengine.ext import db
from google.appengine.ext import deferred

import config
import generators
import markup
import static
import utils


if config.default_markup in markup.MARKUP_MAP:
  DEFAULT_MARKUP = config.default_markup
else:
  DEFAULT_MARKUP = 'html'


class BlogDate(db.Model):
  """Contains a list of year-months for published blog posts."""

  @classmethod
  def get_key_name(cls, post):
    return '%d/%02d' % (post.published_tz.year, post.published_tz.month)

  @classmethod
  def create_for_post(cls, post):
    inst = BlogDate(key_name=BlogDate.get_key_name(post))
    inst.put()
    return inst

  @classmethod
  def datetime_from_key_name(cls, key_name):
    year, month = key_name.split("/")
    return datetime.datetime(int(year), int(month), 1, tzinfo=utils.tzinfo())

  @property
  def date(self):
    return BlogDate.datetime_from_key_name(self.key().name()).date()


class BlogPost(db.Model):
  # The URL path to the blog post. Posts have a path iff they are published.
  path = db.StringProperty()
  title = db.StringProperty(required=True, indexed=False)
  body_markup = db.StringProperty(choices=set(markup.MARKUP_MAP),
                                  default=DEFAULT_MARKUP)
  body = db.TextProperty(required=True)
  tags = aetycoon.SetProperty(basestring, indexed=False)
  published = db.DateTimeProperty()
  updated = db.DateTimeProperty(auto_now=False)
  deps = aetycoon.PickleProperty()

  @property
  def published_tz(self):
    return utils.tz_field(self.published)

  @property
  def updated_tz(self):
    return utils.tz_field(self.updated)

  @aetycoon.TransformProperty(tags)
  def normalized_tags(tags):
    return list(set(utils.slugify(x.lower()) for x in tags))

  @property
  def tag_pairs(self):
    return [(x, utils.slugify(x.lower())) for x in self.tags]

  @property
  def rendered(self):
    """Returns the rendered body."""
    return markup.render_body(self)

  @property
  def summary(self):
    """Returns a summary of the blog post."""
    return markup.render_summary(self)

  @property
  def hash(self):
    val = (self.title, self.body, self.published)
    return hashlib.sha1(str(val)).hexdigest()

  @property
  def summary_hash(self):
    val = (self.title, self.summary, self.tags, self.published)
    return hashlib.sha1(str(val)).hexdigest()
    
  @property
  def tags_hash(self):
    """ Hash of tags only, used by TagCloudContentGenerator
    @author Tom Allen """
    val = (self.tags)
    return hashlib.sha1(str(val)).hexdigest()

  def publish(self):
    regenerate = False
    if not self.path:
      num = 0
      content = None
      while not content:
        """ Tries to find a unique URL for this post, and adds the content
        to the datastore when it finds one. """
        path = utils.format_post_path(self, num)
        content = static.add(path, '', config.html_mime_type)
        num += 1
      self.path = path
      self.put()
      # Force regenerate on new publish. Also helps with generation of
      # chronologically previous and next page.
      regenerate = True

    # Create BlogDate and TagCounter objects given the data for this post.
    BlogDate.create_for_post(self)
    TagCounter.create_for_post(self)

    """ For every type of generated content (indexes, tags, etc) dependent
    upon this particular post:
    i) Fetch the current list of resources and etag from the current
    ContentGenerator
    ii) Fetch the stored list of resources and etag from self.deps
    iii) If the etag has changed, we need to regenerate all resources - so we
    set to_regenerate to the union of the old and new resources.
    iv) If the etag has not changed, we only need to regenerate added or
    removed resources - so we set to_regenerate to the symmetric difference of
    the old and new resources.
    v) For each resource that needs regenerating, we call generate_resource().
    vi) Finally, we update the BlogPost's list of deps with the new set of
    resources and etag.
    
    Later edit: The only change here is that we check if the ContentGenerator
    permits deferred execution. If it doesn't, we execute generate_resource
    as normal, but if it does, we call deferred.defer for each changed
    dependency.
    
    (Some of this sequence is now encapsulated in other functions, but it's
    doing roughly the same thing still.)
    """
    for generator_class, deps in self.get_deps(regenerate=regenerate):
      for dep in deps:
        if generator_class.can_defer:
          deferred.defer(generator_class.generate_resource, None, dep)
        else:
          generator_class.generate_resource(self, dep)
    self.put()

  def remove(self):
    if not self.is_saved():
      return
    # It is important that the get_deps() return the post dependency
    # before the list dependencies as the BlogPost entity gets deleted
    # while calling PostContentGenerator.
    for generator_class, deps in self.get_deps(regenerate=True):
      for dep in deps:
        if generator_class.can_defer:
          deferred.defer(generator_class.generate_resource, None, dep)
        else:
          if generator_class.name() == 'PostContentGenerator':
            generator_class.generate_resource(self, dep, action='delete')
            self.delete()
          else:
            generator_class.generate_resource(self, dep)

  def get_deps(self, regenerate=False):
    if not self.deps:
      self.deps = {}
    for generator_class in generators.generator_list:
      new_deps = set(generator_class.get_resource_list(self))
      new_etag = generator_class.get_etag(self)
      old_deps, old_etag = self.deps.get(generator_class.name(), (set(), None))
      if new_etag != old_etag or regenerate:
        # If the etag has changed, regenerate everything
        to_regenerate = new_deps | old_deps
      else:
        # Otherwise just regenerate the changes
        to_regenerate = new_deps ^ old_deps
      self.deps[generator_class.name()] = (new_deps, new_etag)
      yield generator_class, to_regenerate

class Page(db.Model):
  # The URL path to the page.
  path = db.StringProperty(required=True)
  title = db.TextProperty(required=True)
  template = db.StringProperty(required=True)
  body = db.TextProperty(required=True)
  created = db.DateTimeProperty(required=True, auto_now_add=True)
  updated = db.DateTimeProperty()

  @property
  def rendered(self):
    # Returns the rendered body.
    return markup.render_body(self)

  @property
  def hash(self):
    val = (self.path, self.body, self.published)
    return hashlib.sha1(str(val)).hexdigest()

  def publish(self):
    self._key_name = self.path
    self.put()
    generators.PageContentGenerator.generate_resource(self, self.path);

  def remove(self):
    if not self.is_saved():   
      return
    self.delete()
    generators.PageContentGenerator.generate_resource(self, self.path, action='delete')

class VersionInfo(db.Model):
  bloggart_major = db.IntegerProperty(required=True)
  bloggart_minor = db.IntegerProperty(required=True)
  bloggart_rev = db.IntegerProperty(required=True)

  @property
  def bloggart_version(self):
    return (self.bloggart_major, self.bloggart_minor, self.bloggart_rev)

class TagCounter(db.Model):
  tagname = db.StringProperty(required=True)
  tagcount = db.IntegerProperty(required=True, default=0)

  @property
  def tag_and_count(self):
      return (utils.slugify(self.tagname.lower()), self.tagcount)
      
  @classmethod
  def create_for_post(cls, post):
    import logging
    for tag in post.normalized_tags:
      logging.debug('TagCounter.create_for_post in models.py, tag = ' + tag)
      inst = TagCounter.get_by_key_name(key_names=str(tag))
      if inst is None:
        inst = TagCounter(key_name=str(tag), tagname=str(tag), tagcount=0)
      inst.tagcount += 1
      inst.put()
      