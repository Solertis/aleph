import logging
from hashlib import sha1
from sqlalchemy import func
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm.attributes import flag_modified

from aleph.core import db
from aleph.metadata import Metadata
from aleph.data.validate import validate
from aleph.model.collection import Collection
from aleph.model.reference import Reference
from aleph.model.common import DatedModel
from aleph.text import string_value

log = logging.getLogger(__name__)


class Document(db.Model, DatedModel):
    _schema = 'document.json#'

    TYPE_TEXT = 'text'
    TYPE_TABULAR = 'tabular'
    TYPE_OTHER = 'other'

    id = db.Column(db.BigInteger, primary_key=True)
    content_hash = db.Column(db.Unicode(65), nullable=False, index=True)
    foreign_id = db.Column(db.Unicode, unique=False, nullable=True)
    type = db.Column(db.Unicode(10), nullable=False, index=True)
    _meta = db.Column('meta', JSONB)

    collection_id = db.Column(db.Integer, db.ForeignKey('collection.id'), nullable=False, index=True)  # noqa
    collection = db.relationship(Collection, backref=db.backref('documents', lazy='dynamic'))

    @property
    def title(self):
        return self.meta.title

    @hybrid_property
    def meta(self):
        self._meta = self._meta or {}
        self._meta['content_hash'] = self.content_hash
        self._meta['foreign_id'] = self.foreign_id
        return Metadata.from_data(self._meta or {})

    @meta.setter
    def meta(self, meta):
        if isinstance(meta, Metadata):
            self.content_hash = meta.content_hash
            self.foreign_id = meta.foreign_id
            meta = meta.to_attr_dict()
        self._meta = meta
        flag_modified(self, '_meta')

    def update(self, data):
        validate(data, self._schema)
        meta = self.meta
        meta.update(data, safe=True)
        self.meta = meta
        db.session.add(self)

    def delete_pages(self):
        pq = db.session.query(DocumentPage)
        pq = pq.filter(DocumentPage.document_id == self.id)
        pq.delete(synchronize_session='fetch')
        db.session.refresh(self)

    def delete_records(self):
        pq = db.session.query(DocumentRecord)
        pq = pq.filter(DocumentRecord.document_id == self.id)
        pq.delete(synchronize_session='fetch')
        db.session.refresh(self)

    def delete_references(self, origin=None):
        pq = db.session.query(Reference)
        pq = pq.filter(Reference.document_id == self.id)
        if origin is not None:
            pq = pq.filter(Reference.origin == origin)
        pq.delete(synchronize_session='fetch')
        db.session.refresh(self)

    def delete(self, deleted_at=None):
        self.delete_references()
        self.delete_records()
        self.delete_pages()
        db.session.delete(self)

    def insert_records(self, sheet, iterable, chunk_size=1000):
        chunk = []
        for i, data in enumerate(iterable):
            chunk.append({
                'document_id': self.id,
                'row_id': i,
                'sheet': sheet,
                'data': data
            })
            if len(chunk) >= chunk_size:
                db.session.bulk_insert_mappings(DocumentRecord, chunk)
                chunk = []

        if len(chunk):
            db.session.bulk_insert_mappings(DocumentRecord, chunk)

    def text_parts(self):
        if self.type == self.TYPE_TEXT:
            for page in self.pages:
                for text in page.text_parts():
                    yield text
        elif self.type == self.TYPE_TABULAR:
            for record in self.records:
                for text in record.text_parts():
                    yield text

    @classmethod
    def get_max_id(cls):
        q = db.session.query(func.max(cls.id))
        return q.scalar()

    def __repr__(self):
        return '<Document(%r,%r,%r)>' % (self.id, self.type, self.meta.title)

    def _add_to_dict(self, data):
        try:
            from flask import request
            source_id = self.collection_id
            data['public'] = request.authz.collection_public(source_id)
        except:
            data['public'] = None
        data.update({
            'id': self.id,
            'type': self.type,
            'collection_id': self.collection_id,
            'created_at': self.created_at,
            'updated_at': self.updated_at
        })
        return data

    def to_dict(self):
        data = self.meta.to_dict()
        return self._add_to_dict(data)

    def to_index_dict(self):
        data = self.meta.to_index_dict()
        return self._add_to_dict(data)


class DocumentPage(db.Model):

    id = db.Column(db.BigInteger, primary_key=True)
    number = db.Column(db.Integer(), nullable=False)
    text = db.Column(db.Unicode(), nullable=False)
    document_id = db.Column(db.Integer(), db.ForeignKey('document.id'))
    document = db.relationship(Document, backref=db.backref('pages', cascade='all, delete-orphan'))  # noqa

    def __repr__(self):
        return '<DocumentPage(%r,%r)>' % (self.document_id, self.number)

    def text_parts(self):
        """Utility method to get all text snippets in a record."""
        text = string_value(self.text)
        if text is not None:
            yield self.text

    def to_dict(self):
        return {
            'id': self.id,
            'number': self.number,
            'text': self.text,
            'document_id': self.document_id
        }


class DocumentRecord(db.Model):

    id = db.Column(db.BigInteger, primary_key=True)
    sheet = db.Column(db.Integer, nullable=False)
    row_id = db.Column(db.Integer, nullable=False)
    data = db.Column(JSONB)
    document_id = db.Column(db.Integer(), db.ForeignKey('document.id'))
    document = db.relationship(Document, backref=db.backref('records', cascade='all, delete-orphan'))  # noqa

    @property
    def tid(self):
        tid = sha1(str(self.document_id))
        tid.update(str(self.sheet))
        tid.update(str(self.row_id))
        return tid.hexdigest()

    def text_parts(self):
        """Utility method to get all text snippets in a record."""
        for value in self.data.values():
            text = string_value(value)
            if text is not None:
                yield value

    def __repr__(self):
        return '<DocumentRecord(%r,%r)>' % (self.document_id, self.row_id)
