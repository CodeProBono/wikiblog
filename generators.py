import datetime
import itertools
import os
import urllib
from google.appengine.api import urlfetch
from google.appengine.ext import db
from google.appengine.ext import deferred

import fix_path
import config
import markup
import static
import utils


generator_list = []


class ContentGenerator(object):
  """A class that generates content and dependency lists for blog posts."""

  can_defer = True
  """If True, this ContentGenerator's resources can be generated later."""

  @classmethod
  def name(cls):
    """ returns a unique name for the ContentGenerator.
    By default, this is the name of the class. """
    return cls.__name__

  @classmethod
  def get_resource_list(cls, post):
    """Returns a list of resources (dependencies) for the given post.
    Takes a BlogPost and is expected to return a list of strings representing
    resources this post will appear in. For example, if we were implementing
    tags, this would return a list of tags in the post: ["foo", "bar", "baz"]
    
    TomNote: This is described really badly by the original author, but it
    intended to be read as "this post affects these resources", and is used
    later to say "for all resources that were affected by this post, please
    regenerate your content now" and similar things.
    
    Args:
      post: A BlogPost entity.
    Returns:
      A list of resource strings representing resources affected by this post.
    """
    raise NotImplementedError()

  @classmethod
  def get_etag(cls, post):
    """Returns a string that changes if the resource requires regenerating.

    Args:
      post: A BlogPost entity.
    Returns:
      A string representing the current state of the entity, as relevant to this
      ContentGenerator.
    """
    raise NotImplementedError()

  @classmethod
  def generate_resource(cls, post, resource):
    """(Re)generates a resource for the provided post.

    Args:
      post: A BlogPost entity.
      resource: A resource string as returned by get_resource_list.
    """
    raise NotImplementedError()


class PostContentGenerator(ContentGenerator):
  """ContentGenerator for the actual blog post itself."""

  can_defer = False

  @classmethod
  def get_resource_list(cls, post):
    """ Return the path of this post - because the only PostContent resource
    affected by this post... is this post itself. i.e. When this post changes
    (is edited, for example), the static content that represents it and is
    stored in the data store should be regenerated. """
    return [post.key().id()]

  @classmethod
  def get_etag(cls, post):
    return post.hash

  @classmethod
  def get_prev_next(cls, post):
    """Retrieves the chronologically previous and next post for this post"""
    import models

    q = models.BlogPost.all().order('-published')
    q.filter('published !=', datetime.datetime.max)# Filter drafts out
    q.filter('published <', post.published)
    prev = q.get()

    q = models.BlogPost.all().order('published')
    q.filter('published !=', datetime.datetime.max)# Filter drafts out
    q.filter('published >', post.published)
    next = q.get()

    return prev,next

  @classmethod
  def generate_resource(cls, post, resource, action='post'):
    """ Updates the post in the static serving system.
    Also handles deleting posts if action=='delete' """
    import models
    if not post:
      post = models.BlogPost.get_by_id(resource)
    else:
      assert resource == post.key().id()
    # Handle deletion
    if action == 'delete':
      static.remove(post.path)
      return
    template_vals = {
        'post': post,
    }
    prev, next = cls.get_prev_next(post)
    if prev is not None:
      template_vals['prev']=prev
    if next is not None:
      template_vals['next']=next
    rendered = utils.render_template("post.html", template_vals)
    static.set(post.path, rendered, config.html_mime_type)
generator_list.append(PostContentGenerator)

class PostPrevNextContentGenerator(PostContentGenerator):
  """ContentGenerator for the blog posts chronologically before and after the blog post."""

  @classmethod
  def get_resource_list(cls, post):
    prev, next = cls.get_prev_next(post)
    resource_list = [res.key().id() for res in (prev,next) if res is not None]
    return resource_list

  @classmethod
  def generate_resource(cls, post, resource):
    import models
    post = models.BlogPost.get_by_id(resource)
    if post is None:
      return
    template_vals = {
        'post': post,
    }
    prev, next = cls.get_prev_next(post)
    if prev is not None:
     template_vals['prev']=prev
    if next is not None:
     template_vals['next']=next
    rendered = utils.render_template("post.html", template_vals)
    static.set(post.path, rendered, config.html_mime_type)
generator_list.append(PostPrevNextContentGenerator)

class ListingContentGenerator(ContentGenerator):
  """ ListingContent includes the index, tags, etc """
  path = None
  """The path for listing pages."""

  first_page_path = None
  """The path for the first listing page."""

  @classmethod
  def get_etag(cls, post):
    """ The hash only depends on the summary (title, tags, summary, date),
    not the body content. """
    return post.summary_hash

  @classmethod
  def _filter_query(cls, resource, q):
    """Applies filters to the BlogPost query.

    Args:
      resource: The resource being generated.
      q: The query to act on.
    """
    pass

  @classmethod
  def generate_resource(cls, post, resource, pagenum=1, start_ts=None):
    """ Fetches a list of the N most recent blog posts, and renders the
    listing.html template with them - storing the result to the root path
    (or offset if not the first N posts).
    
    To clarify, the output of the listing template with the template values is
    stored in the static store at a location whose key is this path.
    """
    import models
    q = models.BlogPost.all().order('-published')
    q.filter('published <', start_ts or datetime.datetime.max)
    cls._filter_query(resource, q)

    posts = q.fetch(config.posts_per_page + 1)
    more_posts = len(posts) > config.posts_per_page

    path_args = {
        'resource': resource,
    }
    _get_path = lambda: \
                  cls.first_page_path if path_args['pagenum'] == 1 else cls.path
    path_args['pagenum'] = pagenum - 1
    prev_page = _get_path() % path_args
    path_args['pagenum'] = pagenum + 1
    next_page = cls.path % path_args
    template_vals = {
        'generator_class': cls.__name__,
        'posts': posts[:config.posts_per_page],
        'prev_page': prev_page if pagenum > 1 else None,
        'next_page': next_page if more_posts else None,
    }
    rendered = utils.render_template("listing.html", template_vals)

    path_args['pagenum'] = pagenum
    static.set(_get_path() % path_args, rendered, config.html_mime_type)
    if more_posts:
        deferred.defer(cls.generate_resource, None, resource, pagenum + 1,
                       posts[-2].published)


class IndexContentGenerator(ListingContentGenerator):
  """ContentGenerator for the homepage of the blog and archive pages."""

  path = '/page/%(pagenum)d'
  first_page_path = '/'

  @classmethod
  def get_resource_list(cls, post):
    """ The only IndexContent that depends on this post is the index itself. """
    return ["index"]
generator_list.append(IndexContentGenerator)


class TagsContentGenerator(ListingContentGenerator):
  """ContentGenerator for the tags pages."""

  path = '/tag/%(resource)s/%(pagenum)d'
  first_page_path = '/tag/%(resource)s'

  @classmethod
  def get_resource_list(cls, post):
    return post.normalized_tags

  @classmethod
  def _filter_query(cls, resource, q):
    q.filter('normalized_tags =', resource)
generator_list.append(TagsContentGenerator)


class ArchivePageContentGenerator(ListingContentGenerator):
  """
  ContentGenerator for archive pages (a list of posts in a certain
  year-month).
  """

  path = '/archive/%(resource)s/%(pagenum)d'
  first_page_path = '/archive/%(resource)s/'

  @classmethod
  def get_resource_list(cls, post):
    from models import BlogDate
    return [BlogDate.get_key_name(post)]

  @classmethod
  def _filter_query(cls, resource, q):
    from models import BlogDate
    ts = BlogDate.datetime_from_key_name(resource)

    # We don't have to bother clearing hour, min, etc., as
    # datetime_from_key_name() only sets the year and month.
    min_ts = ts.replace(day=1)

    # Make the next month the upperbound.
    # Python doesn't wrap the month for us, so handle it manually.
    if min_ts.month >= 12:
      max_ts = min_ts.replace(year=min_ts.year+1, month=1)
    else:
      max_ts = min_ts.replace(month=min_ts.month+1)

    q.filter('published >=', min_ts)
    q.filter('published <', max_ts)
generator_list.append(ArchivePageContentGenerator)


class ArchiveIndexContentGenerator(ContentGenerator):
  """
  ContentGenerator for archive index (a list of year-month pairs).
  """

  @classmethod
  def get_resource_list(cls, post):
    return ["archive"]

  @classmethod
  def get_etag(cls, post):
    return post.hash

  @classmethod
  def generate_resource(cls, post, resource):
    from models import BlogDate

    q = BlogDate.all().order('-__key__')
    dates = [x.date for x in q]
    date_struct = {}
    for date in dates:
      date_struct.setdefault(date.year, []).append(date)

    str = utils.render_template("archive.html", {
      'generator_class': cls.__name__,
      'dates': dates,
      'date_struct': date_struct.values(),
    })
    static.set('/archive/', str, config.html_mime_type)
generator_list.append(ArchiveIndexContentGenerator)


class AtomContentGenerator(ContentGenerator):
  """ContentGenerator for Atom feeds."""

  @classmethod
  def get_resource_list(cls, post):
    return ["atom"]

  @classmethod
  def get_etag(cls, post):
    return post.hash

  @classmethod
  def generate_resource(cls, post, resource):
    import models
    q = models.BlogPost.all().order('-updated')
    # Fetch the 10 most recently updated non-draft posts
    posts = list(itertools.islice((x for x in q if x.path), 10))
    now = datetime.datetime.now().replace(second=0, microsecond=0)
    template_vals = {
        'posts': posts,
        'updated': now,
    }
    rendered = utils.render_template("atom.xml", template_vals)
    static.set('/feeds/atom.xml', rendered,
               'application/atom+xml; charset=utf-8', indexed=False,
               last_modified=now)
    if config.hubbub_hub_url:
      cls.send_hubbub_ping(config.hubbub_hub_url)

  @classmethod
  def send_hubbub_ping(cls, hub_url):
    data = urllib.urlencode({
        'hub.url': 'http://%s/feeds/atom.xml' % (config.host,),
        'hub.mode': 'publish',
    })
    response = urlfetch.fetch(hub_url, data, urlfetch.POST)
    if response.status_code / 100 != 2:
      raise Exception("Hub ping failed", response.status_code, response.content)
generator_list.append(AtomContentGenerator)

class PageContentGenerator(ContentGenerator):
  @classmethod
  def generate_resource(cls, page, resource, action='post'):
    # Handle deletion
    if action == 'delete':
      static.remove(page.path)
    else:
      template_vals = {
          'page': page,
      }
      rendered = utils.render_template('pages/%s' % (page.template,),
                                       template_vals)
      static.set(page.path, rendered, config.html_mime_type)

#class TagCloudContentGenerator(ContentGenerator):
#  """ContentGenerator for tag clouds.
#  @author Tom Allen
#  """
#
#  @classmethod
#  def get_resource_list(cls, post):
#    """ Return the tags for this post. """
#    return post.normalized_tags
#
#  @classmethod
#  def get_etag(cls, post):
#    return post.tags_hash # Uses tags only to generate the hash.
#
#  """
#  @classmethod
#  def _filter_query(cls, resource, q):
#    q.filter('normalized_tags =', resource)
#  """
#
#  @classmethod
#  def generate_resource(cls, post, resource):
#    """ Updates the tag cloud in the static serving system. """
#    
#    for tag in resource:
#      def tx():
#        import models
#        tag_counter = TagCounter.get_by_key_name( tag )
#        if tag_counter is None:
#          tag_counter = TagCounter(key_name=tag)
#        counter.count += 1
#        counter.tag = tag
#        counter.url = utils.slugify( tag )
#        counter.put()
#      db.run_in_transaction(tx)
#    
#    """ I need to create a new model thingy earlier and add to its count
#    whenever a new post is made. Count up front, rather than trying to count
#    when displaying. Thus, this function would probably ignore the resource
#    variable, because it's going to access another entity instead. We can still
#    recreate the tag cloud HTML via this static method, but don't try to query
#    all blog posts and count the tags here..."""
#    
#    # Build triples of (tag, url, count)
#    tag_pairs_and_counts = [];
#    
#    template_vals = {
#      'tag_pairs_and_counts': tag_pairs_and_counts,
#    }
#    
#    rendered = utils.render_template("tagcloud.html", template_vals)
#    static.set('/tagcloud.html', rendered, config.html_mime_type)
#generator_list.append(TagCloudContentGenerator)