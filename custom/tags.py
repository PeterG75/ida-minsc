"""
Tags module

This module exposes tools for exporting the currently defined tags
within the database. Once exported, these tags can then be pickled
or then re-applied to the same or another database. Some options
are allowed which will let a user apply translations to the tags
before applying them to a target database.

To fetch all of the tags from the database::

    > res = custom.tags.read()

To export only specific tags from the database::

    > res = custom.tags.export('tag1', 'tag2', ...)

To apply previously read tags to the database::

    > custom.tags.apply(res)

To apply previously read tags with different names to the database::

    > custom.tags.apply(res, tag1='my_tag1', tag2='my_tag2', ...)

"""

import six, sys, logging
import functools, operator, itertools, types, string

import database as db, function as func, structure as struc, ui
import internal

output = sys.stderr

### miscellaneous tag utilities
def list():
    '''Return the tags for the all of the function contents within the database as a set.'''
    return {res for res in itertools.chain(*(res for _, res in db.selectcontents()))}

### internal utility functions and classes
def lvarNameQ(name):
    '''Determine whether a ``name`` is something that IDA named automatically.'''
    if any(name.startswith(n) for n in ('arg_', 'var_')):
        res = name.split('_', 2)[-1]
        return all(n in string.hexdigits for n in res)
    elif name.startswith(' '):
        return name[1:] in {'s', 'r'}
    return False

def locationToAddress(loc):
    '''Convert the function location ``loc`` back into an address.'''

    ## if location is a tuple, then convert it to an address
    if isinstance(loc, tuple):
        f, cid, ofs = loc
        base, _ = next(b for i, b in enumerate(func.chunks(f)) if i == cid)
        return base + ofs

    ## otherwise, it's already an address
    return loc

def addressToLocation(ea, chunks=None):
    """Convert the address ``ea`` to a `(function, id, offset)`.

    The fields `id` and `offset` represent the chunk index and the
    offset into the chunk for the function at ``ea``. If the list
    ``chunks`` is specified as a parameter, then use it as a tuple
    of ranges in order to calculate the correct address.
    """
    F, chunks = func.by(ea), chunks or [ch for ch in func.chunks(ea)]
    cid, base = next((i, l) for i, (l, r) in enumerate(chunks) if l <= ea < r)
    return func.top(F), cid, ea - base

class dummy(object):
    """
    A dummy object that is guaranteed to return False whenever it is compared
    against anything.
    """
    def __eq__(self, other): return False
    def __cmp__(self, other): return -1
dummy = dummy()

### read without using the tag cache
class read(object):
    """
    This namespace contains tools that can be used to manually read
    tags out of the database without using the cache.

    If ``location`` is specified as true, then read each contents tag
    according to its location rather than an address. This allows one
    to perform a translation of the tags in case the function chunks
    are at different addresses than when the tags were read.
    """

    def __new__(cls, location=False):
        '''Read all of the tags defined within the database.'''
        return cls.everything(location=location)

    ## reading the content from a function
    @classmethod
    def content(cls, ea):
        '''Iterate through every tag belonging to the contents of the function at ``ea``.'''
        F = func.by(ea)

        # iterate through every address in the function
        for ea in func.iterate(F):
            ui.navigation.set(ea)

            # yield the tags
            res = db.tag(ea)
            if res: yield ea, res
        return

    ## reading the tags from a frame
    @classmethod
    def frame(cls, ea):
        '''Iterate through each field within the frame belonging to the function ``ea``.'''
        F = func.by(ea)

        # iterate through all of the frame's members
        res = func.frame(F)
        for member in res.members:
            # if ida has named it and there's no comment, then skip
            if lvarNameQ(member.name) and not member.comment:
                continue

            # if it's a structure, then the type is the structure name
            if isinstance(member.type, struc.structure_t):
                logging.debug("{:s}.frame({:#x}) : Storing structure-based type as name for field {:+#x} with tne type {!s}.".format('.'.join((__name__, cls.__name__)), ea, member.offset, member.type))
                type = member.type.name

            # otherwise, the type is a tuple that we can serializer
            else:
                type = member.type

            # otherwise, it's just a regular field. so we can just save what's important.
            yield member.offset, (member.name, type, member.comment)
        return

    ## reading everything from the entire database
    @classmethod
    def everything(cls, location=False):
        """Read all of the tags defined within the database.

        Returns a tuple of the format `(Globals, Contents, Frames)`. Each field
        is a dictionary keyed by location or offset that retains the tags that
        were read. If the boolean ``location`` was specified then key each
        contents tag by location instead of address.
        """
        global read

        # read the globals and the contents
        print >>output, '--> Grabbing globals...'
        Globals = { ea : res for ea, res in read.globals() }

        # read all content
        print >>output, '--> Grabbing contents from all functions...'
        Contents = { loc : res for loc, res in read.contents(location=location) }

        # read the frames
        print >>output, '--> Grabbing frames from all functions...'
        Frames = {ea : res for ea, res in read.frames()}

        # return everything back to the user
        return Globals, Contents, Frames

    ## reading the globals from the database
    @staticmethod
    def globals():
        '''Iterate through all of the tags defined globally witin the database.'''
        ea, sentinel = db.config.bounds()

        # loop till we hit the end of the database
        while ea < sentinel:
            ui.navigation.auto(ea)
            funcQ = func.within(ea)

            # figure out which tag function to use
            f = func.tag if funcQ else db.tag

            # grab the tag and yield it
            res = f(ea)
            if res: yield ea, res

            # if we're in a function, then seek to the next chunk
            if funcQ:
                _, ea = func.chunk(ea)
                continue

            # otherwise, try the next address till we hit a sentinel value
            try: ea = db.a.next(ea)
            except internal.exceptions.OutOfBoundsError: ea = sentinel
        return

    ## reading the contents from the entire database
    @staticmethod
    def contents(location=False):
        """Iterate through the contents tags for all the functions within the database.

        Each iteration yields a tuple of the format `(location, tags)` where
        `location` can be either an address or a chunk identifier and offset
        depending on whether ``location`` was specified as true or not.
        """
        global read

        # Iterate through each function in the database
        for ea in db.functions():

            # it's faster to precalculate the chunks here
            F, chunks = func.by(ea), [ch for ch in func.chunks(ea)]

            # Iterate through the function's contents yielding each tag
            for ea, res in read.content(ea):
                loc = addressToLocation(ea, chunks=chunks) if location else ea
                yield loc, res
            continue
        return

    ## reading the frames from the entire database
    @staticmethod
    def frames():
        '''Iterate through the fields of each frame for all the functions defined within the database.'''
        global read

        for ea in db.functions():
            ui.navigation.procedure(ea)
            res = dict(read.frame(ea))
            if res: yield ea, res
        return

### Applying tags to the database
class apply(object):
    """
    This namespace contains tools that can be used to apply tags that
    have been previously read back into the database.

    Various functions defined within this namespace take a variable number of
    keyword arguments which represent a mapping for the tag names. When a
    tag name was specified, this mapping will be used to rename the tags
    before actually writing them back into the database.
    """

    def __new__(cls, (Globals, Contents, Frames), **tagmap):
        '''Apply the tags in the argument `(Globals, Contents, Frames)` back into the database.'''
        res = Globals, Contents, Frames
        return cls.everything(res, **tagmap)

    ## applying the content to a function
    @classmethod
    def content(cls, Contents, **tagmap):
        '''Apply ``Contents`` back into a function's contents within the database.'''
        global apply
        return apply.contents(Contents, **tagmap)

    ## applying a frame to a function
    @classmethod
    def frame(cls, ea, frame, **tagmap):
        '''Apply the fields from ``frame`` back into the function at ``ea``.'''
        tagmap_output = ", {:s}".format(', '.join("{:s}={:s}".format(k, v) for k, v in six.iteritems(tagmap))) if tagmap else ''

        F = func.frame(ea)
        for offset, (name, type, comment) in six.iteritems(frame):
            try:
                member = F.by_offset(offset)
            except internal.exceptions.MemberNotFoundError:
                logging.warn("{:s}.frame({:#x}, ...{:s}) : Unable to find frame member at {:+#x}. Skipping application of the name ({!r}), type ({!r}), and comment ({!r}) to it.".format('.'.join((__name__, cls.__name__)), ea, tagmap_output, offset, name, type, comment))
                continue

            if member.name != name:
                if any(not member.name.startswith(n) for n in ('arg_', 'var_', ' ')):
                    logging.warn("{:s}.frame({:#x}, ...{:s}) : Renaming frame member {:+#x} from the name {!r} to {!r}.".format('.'.join((__name__, cls.__name__)), ea, tagmap_output, offset, member.name, name))
                member.name = name

            # check what's going to be overwritten with different values prior to doing it
            state, res = map(internal.comment.decode, (member.comment, comment))

            # transform the new tag state using the tagmap
            new = { tagmap.get(name, name) : value for name, value in six.viewitems(res) }

            # check if the tag mapping resulted in the deletion of a tag
            if len(new) != len(res):
                for name in six.viewkeys(res) - six.viewkeys(new):
                    logging.warn("{:s}.frame({:#x}, ...{:s}) : Refusing requested tag mapping as it results in the tag {!r} overwriting tag {!r} for the frame member {:+#x}. The value {!r} would be overwritten by {!r}.".format('.'.join((__name__, cls.__name__)), ea, tagmap_output, name, tagmap[name], offset, res[name], res[tagmap[name]]))
                pass

            # warn the user about what's going to be overwritten prior to doing it
            for name in six.viewkeys(state) & six.viewkeys(new):
                if state[name] == new[name]: continue
                logging.warn("{:s}.frame({:#x}, ...{:s}) : Overwriting tag {!r} for frame member {:+#x} with new value {!r}. The old value was {!r}.".format('.'.join((__name__, cls.__name__)), ea, tagmap_output, name, offset, new[name], state[name]))

            # now we can update the current dictionary
            mapstate = { name : value for name, value in six.iteritems(new) if state.get(name, dummy) != value }
            state.update(mapstate)

            # convert it back to a multi-lined comment and assign it
            member.comment = internal.comment.encode(state)

            # if the type is a string, then figure out which structure to use
            if isinstance(type, basestring):
                try:
                    member.type = struc.by(type)
                except internal.exceptions.StructureNotFoundError:
                    logging.warn("{:s}.frame({:#x}, ...{:s}): Unable to find structure {!r} for member at {:+#x}. Skipping it.".format('.'.join((__name__, cls.__name__)), ea, tagmap_output, type, offset))

            # otherwise, it's a pythonic tuple that we can just assign
            else:
                member.type = type
            continue
        return

    ## apply everything to the entire database
    @classmethod
    def everything(cls, (Globals, Contents, Frames), **tagmap):
        '''Apply the tags in the argument `(Globals, Contents, Frames)` back into the database.'''
        global apply

        ## convert a sorted list keyed by an address into something that updates ida's navigation pointer
        def update_navigation(xs, setter):
            '''Call ``setter`` on ea for each iteration of list ``xs``.'''
            for x in xs:
                ea, _ = x
                setter(ea)
                yield x
            return

        ## convert a sorted list keyed by a location into something that updates ida's navigation pointer
        def update_navigation_contents(xs, setter):
            '''Call ``setter`` on location for each iteration of list ``xs``.'''
            for x in xs:
                loc, _ = x
                ea = locationToAddress(loc)
                setter(ea)
                yield x
            return

        ## handle globals
        print >>output, "--> Writing globals... ({:d} entr{:s})".format(len(Globals), 'y' if len(Globals) == 1 else 'ies')
        iterable = sorted(six.iteritems(Globals), key=operator.itemgetter(0))
        res = apply.globals(update_navigation(iterable, ui.navigation.auto), **tagmap)
        # FIXME: verify that res matches number of Globals

        ## handle contents
        print >>output, "--> Writing function contents... ({:d} entr{:s})".format(len(Contents), 'y' if len(Contents) == 1 else 'ies')
        iterable = sorted(six.iteritems(Contents), key=operator.itemgetter(0))
        res = apply.contents(update_navigation_contents(iterable, ui.navigation.set), **tagmap)
        # FIXME: verify that res matches number of Contents

        ## update any frames
        print >>output, "--> Applying frames to each function... ({:d} entr{:s})".format(len(Frames), 'y' if len(Frames) == 1 else 'ies')
        iterable = sorted(six.iteritems(Frames), key=operator.itemgetter(0))
        res = apply.frames(update_navigation(iterable, ui.navigation.procedure), **tagmap)
        # FIXME: verify that res matches number of Frames

        return

    ## applying tags to the globals
    @staticmethod
    def globals(Globals, **tagmap):
        '''Apply the tags in ``Globals`` back into the database.'''
        global apply
        cls, tagmap_output = apply.__class__, ", {:s}".format(', '.join("{:s}={:s}".format(oldtag, newtag) for oldtag, newtag in six.iteritems(tagmap))) if tagmap else ''

        count = 0
        for ea, res in Globals:
            ns = func if func.within(ea) else db

            # grab the current (old) tag state
            state = ns.tag(ea)

            # transform the new tag state using the tagmap
            new = { tagmap.get(name, name) : value for name, value in six.viewitems(res) }

            # check if the tag mapping resulted in the deletion of a tag
            if len(new) != len(res):
                for name in six.viewkeys(res) - six.viewkeys(new):
                    logging.warn("{:s}.globals(...{:s}) : Refusing requested tag mapping as it results in the tag {!r} overwriting the tag {!r} in the global {:#x}. The value {!r} would be replaced with {!r}.".format('.'.join((__name__, cls.__name__)), tagmap_output, name, tagmap[name], ea, res[name], res[tagmap[name]]))
                pass

            # check what's going to be overwritten with different values prior to doing it
            for name in six.viewkeys(state) & six.viewkeys(new):
                if state[name] == new[name]: continue
                logging.warn("{:s}.globals(...{:s}) : Overwriting tag {!r} for global at {:#x} with new value {!r}. Old value was {!r}.".format('.'.join((__name__, cls.__name__)), tagmap_output, name, ea, new[name], state[name]))

            # now we can apply the tags to the global address
            try:
                [ ns.tag(ea, name, value) for name, value in six.iteritems(new) if state.get(name, dummy) != value ]
            except:
                logging.warn("{:s}.globals(...{:s}) : Unable to apply tags ({!r}) to global {:#x}.".format('.'.join((__name__, cls.__name__)), tagmap_output, new, ea), exc_info=True)

            # increase our counter
            count += 1
        return count

    ## applying contents tags to all the functions
    @staticmethod
    def contents(Contents, **tagmap):
        '''Apply the tags in ``Contents`` back into each function within the database.'''
        global apply
        cls, tagmap_output = apply.__class__, ", {:s}".format(', '.join("{:s}={:s}".format(oldtag, newtag) for oldtag, newtag in six.iteritems(tagmap))) if tagmap else ''

        count = 0
        for loc, res in Contents:
            ea = locationToAddress(loc)

            # warn the user if this address is not within a function
            if not func.within(ea):
                logging.warn("{:s}.contents(...{:s}) : Address {:#x} is not within a function. Using a global tag.".format('.'.join((__name__, cls.__name__)), tagmap_output, ea))

            # grab the current (old) tag state
            state = db.tag(ea)

            # transform the new tag state using the tagmap
            new = { tagmap.get(name, name) : value for name, value in six.viewitems(res) }

            # check if the tag mapping resulted in the deletion of a tag
            if len(new) != len(res):
                for name in six.viewkeys(res) - six.viewkeys(new):
                    logging.warn("{:s}.contents(...{:s}) : Refusing requested tag mapping as it results in the tag {!r} overwriting tag {!r} for the contents at {:#x}. The value {!r} would be overwritten by {!r}.".format('.'.join((__name__, cls.__name__)), tagmap_output, name, tagmap[name], ea, res[name], res[tagmap[name]]))
                pass

            # inform the user if any tags are being overwritten with different values
            for name in six.viewkeys(state) & six.viewkeys(new):
                if state[name] == new[name]: continue
                logging.warn("{:s}.contents(...{:s}) : Overwriting contents tag {!r} for address {:#x} with new value {!r}. Old value was {!r}.".format('.'.join((__name__, cls.__name__)), tagmap_output, name, ea, new[name], state[name]))

            # write the tags to the contents address
            try:
                [ db.tag(ea, name, value) for name, value in six.iteritems(new) if state.get(name, dummy) != value ]
            except:
                logging.warn("{:s}.contents(...{:s}) : Unable to apply tags {!r} to location {:#x}.".format('.'.join((__name__, cls.__name__)), tagmap_output, new, ea), exc_info=True)

            # increase our counter
            count += 1
        return count

    ## applying frames to all the functions
    @staticmethod
    def frames(Frames, **tagmap):
        '''Apply the fields from ``Frames`` back into each function's frame.'''
        global apply
        cls, tagmap_output = apply.__class__, ", {:s}".format(', '.join("{:s}={:s}".format(oldtag, newtag) for oldtag, newtag in six.iteritems(tagmap))) if tagmap else ''

        count = 0
        for ea, res in Frames:
            try:
                apply.frame(ea, res, **tagmap)
            except:
                logging.warn("{:s}.frames(...{:s}) : Unable to apply tags ({!r}) to frame at {:#x}.".format('.'.join((__name__, cls.__name__)), tagmap_output, res, ea), exc_info=True)

            # increase our counter
            count += 1
        return count

### Exporting tags from the database using the tag cache
class export(object):
    """
    This namespace contains tools that can be used to quickly
    export specific tagss out of the database using the cache.

    If ``location`` is specified as true, then read each contents tag
    according to its location rather than an address. This allows one
    to perform a translation of the tags in case the function chunks
    are at different addresses than when the tags were read.
    """

    def __new__(cls, *tags, **location):
        '''Read the specified tags within the database using the cache.'''
        return cls.everything(*tags, **location)

    ## query the content from a function
    @classmethod
    def content(cls, F, *tags, **location):
        '''Iterate through the specified ``tags`` belonging to the contents of the function at ``ea`` using the cache.'''
        identity = lambda res: res
        translate = addressToLocation if location.get('location', False) else identity

        iterable = func.select(F, Or=tags) if tags else func.select(F)
        for ea, res in iterable:
            ui.navigation.set(ea)
            if res: yield translate(ea), res
        return

    ## query the frame from a function
    @classmethod
    def frame(cls, F, *tags):
        '''Iterate through each field containing the specified ``tags`` within the frame belonging to the function ``ea``.'''
        global read, internal
        tags_ = { tag for tag in tags }

        for ofs, item in read.frame(F):
            field, type, comment = item

            # if the entire comment is in tags (like None) or no tags were specified, then save the entire member
            if not tags or comment in tags_:
                yield ofs, item
                continue

            # otherwise, decode the comment into a dictionary using only the tags the user asked for
            comment_ = internal.comment.decode(comment)
            res = { name : comment_[name] for name in six.viewkeys(comment_) & tags_ }

            # if anything was found, then re-encode it and yield to the user
            if res: yield ofs, (field, type, internal.comment.encode(res))
        return

    ## query the entire database for the specified tags
    @classmethod
    def everything(cls, *tags, **location):
        """Read all of the specified ``tags`` within the database using the cache.

        Returns a tuple of the format `(Globals, Contents, Frames)`. Each field
        is a dictionary keyed by location or offset that retains the tags that
        were read. If the boolean ``location`` was specified then key each
        contents tag by location instead of address.
        """
        global export

        # collect all the globals into a dictionary
        print >>output, '--> Grabbing globals (cached)...'
        iterable = export.globals(*tags)
        Globals = {ea : res for ea, res in itertools.ifilter(None, iterable)}

        # grab all the contents into a dictionary
        print >>output, '--> Grabbing contents from functions (cached)...'
        location = location.get('location', False)
        iterable = export.contents(*tags, location=location)
        Contents = {loc : res for loc, res in itertools.ifilter(None, iterable)}

        # grab any frames into a dictionary
        print >>output, '--> Grabbing frames from functions (cached)...'
        iterable = export.frames(*tags)
        Frames = {ea : res for ea, res in itertools.ifilter(None, iterable)}

        # return it back to the user
        return Globals, Contents, Frames

    ## query all the globals matching the specified tags
    @staticmethod
    def globals(*tags):
        '''Iterate through all of the specified global ``tags`` within the database using the cache.'''
        iterable = db.select(Or=tags) if tags else db.select()
        for ea, res in iterable:
            ui.navigation.auto(ea)
            if res: yield ea, res
        return

    ## query all the contents in each function that match the specified tags
    @staticmethod
    def contents(*tags, **location):
        """Iterate through the specified contents ``tags`` within the database using the cache.

        Each iteration yields a tuple of the format `(location, tags)` where
        `location` can be either an address or a chunk identifier and offset
        depending on whether ``location`` was specified as true or not.
        """
        global export
        location = location.get('location', False)

        iterable = db.selectcontents(Or=tags) if tags else db.selectcontents()
        for F, res in iterable:
            for loc, res in export.content(F, *res, location=location):
                if res: yield loc, res
            continue
        return

    ## query all the frames that match the specified tags
    @staticmethod
    def frames(*tags):
        '''Iterate through the fields in each function's frame containing the specified ``tags``.'''
        global export
        tags_ = {x for x in tags}

        for ea in db.functions():
            ui.navigation.procedure(ea)
            res = dict(export.frame(ea, *tags))
            if res: yield ea, res
        return

__all__ = ['list', 'read', 'export', 'apply']
