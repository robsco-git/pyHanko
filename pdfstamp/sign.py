import hashlib
import os
import logging
import struct
import requests
from base64 import b64encode
from datetime import datetime
from dataclasses import dataclass
from enum import IntEnum
from io import BytesIO
from typing import List, Optional

import tzlocal
from pdf_utils import generic, misc
from pdf_utils.generic import pdf_name, pdf_string
from asn1crypto import cms, x509, algos, core, keys, tsp, pem
from oscrypto import asymmetric, keys as oskeys
from oscrypto.errors import SignatureError

from pdf_utils.incremental_writer import (
    IncrementalPdfFileWriter, AnnotAppearances,
)
from pdf_utils.reader import PdfFileReader
from pdfstamp.stamp import TextStampStyle, TextStamp

logger = logging.getLogger(__name__)


ASN_DT_FORMAT = "D:%Y%m%d%H%M%S"


def pdf_date(dt: datetime):
    base_dt = dt.strftime(ASN_DT_FORMAT)
    utc_offset_string = ''
    if dt.tzinfo is not None:
        # compute UTC off set string
        tz_seconds = dt.utcoffset().seconds
        if not tz_seconds:
            utc_offset_string = 'Z'
        else:
            sign = '+'
            if tz_seconds < 0:
                sign = '-'
                tz_seconds = abs(tz_seconds)
            hrs, tz_seconds = divmod(tz_seconds, 3600)
            mins = tz_seconds // 60
            # XXX the apostrophe after the minute part of the offset is NOT
            #  what's in the spec, but Adobe Reader DC refuses to validate
            #  signatures with a date string that doesn't contain it.
            #  No idea why.
            utc_offset_string = sign + ("%02d'%02d'" % (hrs, mins))

    return pdf_string(base_dt + utc_offset_string)


class SigByteRangeObject(generic.PdfObject):

    def __init__(self):
        self._filled = False
        self._range_object_offset = None
        self.first_region_len = 0
        self.second_region_offset = 0
        self.second_region_len = 0

    def fill_offsets(self, stream, sig_start, sig_end, eof):
        if self._filled:
            raise ValueError('Offsets already filled')
        if self._range_object_offset is None:
            raise ValueError(
                'Could not determine where to write /ByteRange value'
            )

        old_seek = stream.tell()
        self.first_region_len = sig_start
        self.second_region_offset = sig_end
        self.second_region_len = eof - sig_end
        # our ArrayObject is rigged to have fixed width
        # so we can just write over it

        stream.seek(self._range_object_offset)
        self.write_to_stream(stream, None)

        stream.seek(old_seek)

    # noinspection PyPep8Naming, PyUnusedLocal
    def write_to_stream(self, stream, encryption_key):
        if self._range_object_offset is None:
            self._range_object_offset = stream.tell()
        string_repr = "[ %08d %08d %08d %08d ]" % (
            0, self.first_region_len,
            self.second_region_offset, self.second_region_len,
        )
        stream.write(string_repr.encode('ascii'))


class PKCS7Placeholder(generic.PdfObject):

    def __init__(self, bytes_reserved=None):
        self._placeholder = True
        self.value = b'0' * (bytes_reserved or 8192)
        self._offsets = None

    @property
    def offsets(self):
        if self._offsets is None:
            raise ValueError('No offsets available')
        return self._offsets

    @property
    def original_bytes(self):
        return self.value

    # always ignore encryption key
    # (I think this is correct, but testing is required)
    # noinspection PyPep8Naming, PyUnusedLocal
    def write_to_stream(self, stream, encryption_key):
        start = stream.tell()
        stream.write(b'<')
        stream.write(self.value)
        stream.write(b'>')
        end = stream.tell()
        if self._offsets is None:
            self._offsets = start, end


# simple PDF signature with two digested regions
# (pre- and post content)
class SignatureObject(generic.DictionaryObject):

    def __init__(self, timestamp: datetime, name=None, location=None,
                 reason=None, bytes_reserved=None):
        # initialise signature object
        super().__init__(
            {
                pdf_name('/Type'): pdf_name('/Sig'),
                pdf_name('/Filter'): pdf_name('/Adobe.PPKLite'),
                pdf_name('/SubFilter'): pdf_name('/adbe.pkcs7.detached'),
                pdf_name('/M'): pdf_date(timestamp)
            }
        )

        if name:
            self[pdf_name('/Name')] = pdf_string(name),
        if location:
            self[pdf_name('/Location')] = pdf_string(location),
        if reason:
            self[pdf_name('/Reason')] = pdf_string(reason),

        # initialise placeholders for /Contents and /ByteRange
        pkcs7 = PKCS7Placeholder(bytes_reserved=bytes_reserved)
        self[pdf_name('/Contents')] = self.signature_contents = pkcs7
        byte_range = SigByteRangeObject()
        self[pdf_name('/ByteRange')] = self.byte_range = byte_range


@dataclass(frozen=True)
class SignatureStatus:
    intact: bool
    valid: bool
    complete_document: bool
    signing_cert: x509.Certificate
    ca_chain: List[x509.Certificate]
    pkcs7_signature_mechanism: str
    md_algorithm: str

    def summary(self):
        if not self.valid:
            return 'FORGED'
        elif self.intact:
            if self.complete_document:
                return 'INTACT_UNTOUCHED'
            else:
                return 'INTACT_EXTENDED'
        else:
            return 'INVALID'


MECHANISMS = (
    'rsassa_pkcs1v15', 'sha1_rsa', 'sha256_rsa', 'sha384_rsa', 'sha512_rsa'
)


def validate_signature(reader: PdfFileReader, sig_object):
    if isinstance(sig_object, generic.IndirectObject):
        sig_object = sig_object.get_object()
    try:
        pkcs7_content = sig_object['/Contents']
        byte_range = sig_object['/ByteRange']
    except KeyError:
        raise ValueError('Signature PDF object is not correctly formatted')
    message = cms.ContentInfo.load(pkcs7_content)
    signed_data = message['content']
    certs = [c.parse() for c in signed_data['certificates']]
    cert = certs[0]
    ca_chain = certs[1:]
    try:
        signer_info, = signed_data['signer_infos']
    except ValueError:
        raise ValueError('signer_infos should contain exactly one entry')

    mechanism = signer_info['signature_algorithm']['algorithm'].native
    md_algorithm = signer_info['digest_algorithm']['algorithm'].native.lower()
    signature = signer_info['signature'].native
    signed_attrs = signer_info['signed_attrs']
    md = getattr(hashlib, md_algorithm)()
    stream = reader.stream
    
    # compute the digest
    old_seek = stream.tell()
    total_len = 0
    for lo, chunk_len in misc.pair_iter(byte_range):
        stream.seek(lo)
        md.update(stream.read(chunk_len))
        total_len += chunk_len
    # compute file size
    stream.seek(0, os.SEEK_END)
    # the * 2 is because of the ASCII hex encoding, and the + 2
    # is the wrapping <>
    embedded_sig_content = len(pkcs7_content) * 2 + 2
    complete_document = stream.tell() == total_len + embedded_sig_content
    stream.seek(old_seek)

    # TODO implement logic to detect whether
    # the modifications made are permissible

    raw_digest = md.digest()
    embedded_digest = None
    for attr in signed_attrs:
        if attr['type'].native == 'message_digest':
            embedded_digest = attr['values'][0].native
    if embedded_digest is None:
        raise ValueError('Unable to locate message digest.') 
    intact = raw_digest == embedded_digest

    # finally validate the signature
    if mechanism not in MECHANISMS:
        raise NotImplementedError(
            'Signature mechanism %s is not currently supported'
            % mechanism
        )
    try:
        # XXX for some reason, these values are sometimes set wrongly
        # when asn1crypto loads things. No clue why, but they mess up
        # the header byte (and hence the signature) of the DER-encoded
        # message object. Needs investigation.
        signed_attrs.class_ = 0
        signed_attrs.tag = 17
        data = signed_attrs.dump(force=True)
        asymmetric.rsa_pkcs1v15_verify(
            asymmetric.load_public_key(cert.public_key), signature, 
            data, hash_algorithm=md_algorithm
        )
        valid = True
    except SignatureError:
        valid = False

    # TODO what about chain-of-trust validation?

    return SignatureStatus(
        intact=intact, complete_document=complete_document,
        ca_chain=ca_chain, valid=valid, signing_cert=cert, 
        md_algorithm=md_algorithm, pkcs7_signature_mechanism=mechanism
    )


class SignatureFormField(generic.DictionaryObject):
    def __init__(self, field_name, include_on_page, *, writer,
                 sig_object_ref=None, box=None,
                 appearances: Optional[AnnotAppearances] = None):

        if box is not None:
            visible = True
            rect = list(map(generic.FloatObject, box))
            if appearances is not None:
                ap = appearances.as_pdf_object()
            else:
                ap = None
        else:
            rect = [generic.FloatObject(0)] * 4
            ap = None
            visible = False

        # this sets the "Print" bit, and activates "Locked" if the
        # signature field is ready to be filled
        flags = 0b100 if sig_object_ref is None else 0b10000100
        super().__init__({
            # Signature field properties
            pdf_name('/FT'): pdf_name('/Sig'),
            pdf_name('/T'): pdf_string(field_name),
            # Annotation properties: bare minimum
            pdf_name('/Type'): pdf_name('/Annot'),
            pdf_name('/Subtype'): pdf_name('/Widget'),
            pdf_name('/F'): generic.NumberObject(flags),
            pdf_name('/P'): include_on_page,
            pdf_name('/Rect'): generic.ArrayObject(rect)
        })
        if sig_object_ref is not None:
            self[pdf_name('/V')] = sig_object_ref
        if ap is not None:
            self[pdf_name('/AP')] = ap

        # register ourselves
        self.reference = self_reference = writer.add_object(self)
        # if we're building an invisible form field, this is all there is to it
        if visible:
            writer.register_annotation(include_on_page, self_reference)


def simple_cms_attribute(attr_type, value):
    return cms.CMSAttribute({
        'type': cms.CMSAttributeType(attr_type),
        'values': (value,)
    })


class Signer:
    signing_cert: x509.Certificate
    ca_chain: List[x509.Certificate]
    pkcs7_signature_mechanism: str
    timestamper: 'Timestamper' = None

    def sign_raw(self, data: bytes, digest_algorithm: str, dry_run=False):
        raise NotImplementedError

    @property
    def subject_name(self):
        name: x509.Name = self.signing_cert.subject
        result = name.native['common_name']
        try:
            email = name.native['email_address']
            result = '%s <%s>' % (result, email)
        except KeyError:
            pass
        return result

    def signed_attrs(self, data_digest: bytes, timestamp: datetime = None):
        timestamp = timestamp or datetime.now(tz=tzlocal.get_localzone())
        return cms.CMSAttributes([
            simple_cms_attribute('content_type', 'data'),
            simple_cms_attribute('message_digest', data_digest),
            simple_cms_attribute(
                'signing_time', cms.Time({'utc_time': core.UTCTime(timestamp)})
            )
            # TODO support adding Adobe-style revocation information
        ])

    def signer_info(self, digest_algorithm: str, signed_attrs, signature):
        digest_algorithm_obj = algos.DigestAlgorithm(
            {'algorithm': digest_algorithm}
        )

        signing_cert = self.signing_cert
        # build the signer info object that goes into the PKCS7 signature
        # (see RFC 2315 § 9.2)
        sig_info = cms.SignerInfo({
            'version': 'v1',
            'sid': cms.SignerIdentifier({
                'issuer_and_serial_number': cms.IssuerAndSerialNumber({
                    'issuer': signing_cert.issuer,
                    'serial_number': signing_cert.serial_number,
                })
            }),
            'digest_algorithm': digest_algorithm_obj,
            # TODO implement PSS support
            'signature_algorithm': algos.SignedDigestAlgorithm(
                {'algorithm': self.pkcs7_signature_mechanism}
            ),
            'signed_attrs': signed_attrs,
            'signature': signature
        })
        if self.timestamper is not None:
            # the timestamp server needs to cross-sign our signature
            md = getattr(hashlib, digest_algorithm)()
            md.update(signature)
            ts_token = self.timestamper.timestamp(md.digest(), digest_algorithm)
            sig_info['unsigned_attrs'] = cms.CMSAttributes([ts_token])
        return sig_info

    def sign(self, data_digest: bytes, digest_algorithm: str,
             timestamp: datetime = None, dry_run=False) -> bytes:

        # Implementation loosely based on similar functionality in
        # https://github.com/m32/endesive/.

        # the piece of data we'll actually sign is a DER-encoded version of the
        # signed attributes of our message
        signed_attrs = self.signed_attrs(data_digest, timestamp)
        signature = self.sign_raw(
            signed_attrs.dump(), digest_algorithm.lower(), dry_run
        )

        sig_info = self.signer_info(digest_algorithm, signed_attrs, signature)

        digest_algorithm_obj = algos.DigestAlgorithm(
            {'algorithm': digest_algorithm}
        )
        # this is the SignedData object for our message (see RFC 2315 § 9.1)
        signed_data = {
            'version': 'v1',
            'digest_algorithms': cms.DigestAlgorithms((digest_algorithm_obj,)),
            'encap_content_info': {'content_type': 'data'},
            'certificates': [self.signing_cert] + self.ca_chain,
            'signer_infos': [sig_info]
        }

        # time to pack up
        message = cms.ContentInfo({
            'content_type': cms.ContentType('signed_data'),
            'content': cms.SignedData(signed_data)
        })

        return message.dump()


@dataclass
class SimpleSigner(Signer):
    signing_cert: x509.Certificate
    ca_chain: List[x509.Certificate]
    signing_key: keys.PrivateKeyInfo
    pkcs7_signature_mechanism: str = 'rsassa_pkcs1v15'

    def sign_raw(self, data: bytes, digest_algorithm: str, dry_run=False):
        return asymmetric.rsa_pkcs1v15_sign(
            asymmetric.load_private_key(self.signing_key),
            data, digest_algorithm.lower()
        )

    @staticmethod
    def load_ca_chain(ca_chain_files):
        for ca_chain_file in ca_chain_files:
            with open(ca_chain_file, 'rb') as f:
                ca_chain_bytes = f.read()
            # use the pattern from the asn1crypto docs
            # to distinguish PEM/DER and read multiple certs
            # from one PEM file (if necessary)
            if pem.detect(ca_chain_bytes):
                pems = pem.unarmor(ca_chain_bytes, multiple=True)
                for type_name, _, der in pems:
                    if type_name is None or type_name.lower() == 'certificate':
                        yield x509.Certificate.load(der)
                    else:
                        logger.debug(
                            f'Skipping PEM block of type {type_name} in '
                            f'{ca_chain_file}.'
                        )
            else:
                # no need to unarmor, just try to load it immediately
                yield x509.Certificate.load(ca_chain_bytes)

    @classmethod
    def load(cls, key_file, cert_file, ca_chain_files=None,
             key_passphrase=None):
        try:
            # load cryptographic data (both PEM and DER are supported)
            with open(key_file, 'rb') as f:
                signing_key: keys.PrivateKeyInfo = oskeys.parse_private(
                    f.read(), password=key_passphrase
                )
            with open(cert_file, 'rb') as f:
                signing_cert: x509.Certificate = oskeys.parse_certificate(
                    f.read()
                )
        except (IOError, ValueError) as e:
            logger.error('Could not load cryptographic material', e)
            return None

        if ca_chain_files:
            try:
                ca_chain = list(SimpleSigner.load_ca_chain(ca_chain_files))
            except (IOError, ValueError) as e:
                logger.error('Could not load CA chain', e)
                return None
        else:
            ca_chain = []

        return SimpleSigner(
            signing_cert=signing_cert, signing_key=signing_key,
            ca_chain=ca_chain
        )


class PKCS11Signer(Signer):

    # TODO is this actually the correct one to use?
    pkcs7_signature_mechanism: str = 'rsassa_pkcs1v15'

    def __init__(self, pkcs11_session, cert_label, ca_chain=None,
                 key_label=None, timestamper=None):
        self.cert_label = cert_label
        self.key_label = key_label or cert_label
        self.pkcs11_session = pkcs11_session
        self.timestamper = timestamper
        self._ca_chain = ca_chain
        self._signing_cert = self._key_handle = None
        self._loaded = False

    @property
    def ca_chain(self):
        # it's conceivable that one might want to load this separately from
        # the key data, so we allow for that.
        if self._ca_chain is None:
            self._ca_chain = self._load_ca_chain()
        return self._ca_chain

    @property
    def signing_cert(self):
        self._load_objects()
        return self._signing_cert

    def sign_raw(self, data: bytes, digest_algorithm: str, dry_run=False):
        if dry_run:
            # allocate 4096 bits for the fake signature
            return b'0' * 512

        self._load_objects()
        from pkcs11 import Mechanism, SignMixin
        kh: SignMixin = self._key_handle
        mech = {
            'sha1': Mechanism.SHA1_RSA_PKCS,
            'sha256': Mechanism.SHA256_RSA_PKCS,
            'sha384': Mechanism.SHA384_RSA_PKCS,
            'sha512': Mechanism.SHA512_RSA_PKCS,
        }[digest_algorithm.lower()]
        return kh.sign(data, mechanism=mech)

    def _load_ca_chain(self):
        return []

    def _load_objects(self):
        if self._loaded:
            return

        from pkcs11 import Attribute, ObjectClass

        q = self.pkcs11_session.get_objects({
            Attribute.LABEL: self.cert_label,
            Attribute.CLASS: ObjectClass.CERTIFICATE
        })
        # need to run through the full iterator to make sure the operation
        # terminates
        cert_obj, = list(q)
        self._signing_cert = oskeys.parse_certificate(cert_obj[Attribute.VALUE])

        self._load_ca_chain()

        q = self.pkcs11_session.get_objects({
            Attribute.LABEL: self.key_label,
            Attribute.CLASS: ObjectClass.PRIVATE_KEY
        })
        self._key_handle, = list(q)

        self._loaded = True


# TODO add more customisability

@dataclass(frozen=True)
class SigFieldSpec:
    sig_field_name: str
    on_page: int = 0
    box: (int, int, int, int) = None

    @property
    def dimensions(self):
        if self.box is not None:
            x1, y1, x2, y2 = self.box
            return abs(x1 - x2), abs(y1 - y2)


class DocMDPPerm(IntEnum):
    """
    Cf. Table 254  in ISO 32000
    """

    NO_CHANGES = 0
    FILL_FORMS = 2
    ANNOTATE = 3


@dataclass(frozen=True)
class PdfSignatureMetadata:
    field_name: str = None
    md_algorithm: str = 'sha512'
    location: str = None
    reason: str = None
    name: str = None
    certify: bool = False
    # only relevant for certification
    docmdp_permissions: DocMDPPerm = DocMDPPerm.FILL_FORMS


def _certification_setup(writer: IncrementalPdfFileWriter,
                         sig_obj_ref, md_algorithm, permission_level):
    """
    Cf. Tables 252, 253 and 254 in ISO 32000
    """
    transform_params = generic.DictionaryObject({
        pdf_name('/Type'): pdf_name('/TransformParams'),
        pdf_name('/V'): pdf_name('/1.2'),
        pdf_name('/P'): generic.NumberObject(permission_level)
    })
    tp_ref = writer.add_object(transform_params)

    # not to be confused with our indirect reference *to* the signature object--
    # this is part of the /Reference entry of the signature object.
    sigref_object = generic.DictionaryObject({
        pdf_name('/Type'): pdf_name('/SigRef'),
        pdf_name('/TransformMethod'): pdf_name('/DocMDP'),
        pdf_name('/DigestMethod'): pdf_name('/' + md_algorithm.upper()),
        pdf_name('/TransformParams'): tp_ref
    })

    # after preparing the sigref object, insert it into the actual signature
    # object under /Reference (for some reason this is supposed to be an array)
    sigref_list = generic.ArrayObject([writer.add_object(sigref_object)])
    sig_obj_ref.get_object()[pdf_name('/Reference')] = sigref_list

    # finally, register a /DocMDP permission entry in the document catalog
    root = writer.root
    # the usual song and dance to grab a reference to /Perms, or create it
    # TODO I've done this enough times to factor it out, I suppose
    try:
        perms_ref = root.raw_get('/Perms')
        if isinstance(perms_ref, generic.IndirectObject):
            perms = perms_ref.get_object()
            writer.mark_update(perms_ref)
        else:
            perms = perms_ref
            writer.update_root()
    except KeyError:
        root[pdf_name('/Perms')] = perms = generic.DictionaryObject()
        writer.update_root()
    perms[pdf_name('/DocMDP')] = sig_obj_ref


def _prepare_sig_field(sig_field_name, root,
                       update_writer: IncrementalPdfFileWriter,
                       existing_fields_only=False, lock_sig_flags=True, 
                       **kwargs):
    if sig_field_name is None:
        raise ValueError

    # Holds a reference to the object containing our form field
    # that we'll have to update IF we create a new form field.
    # In typical situations, this is either the
    # /AcroForm object itself (when its /Fields are a flat, direct array),
    # or whatever /Fields points to.
    field_container_ref = None
    try:
        form_ref = root.raw_get('/AcroForm')

        if isinstance(form_ref, generic.IndirectObject):
            # The /AcroForm exists and is indirect. Hence, we may need to write
            # an update if we end up having to add the signature field
            form = form_ref.get_object()
        else:
            # the form is a direct object, so we'll replace it with
            # an indirect one, and mark the root to be updated
            # (I think this is fairly rare, but requires testing!)
            form = form_ref
            # if updates are not active, we forgo the replacement
            #  operation; in this case, one should only update the
            #  referenced form field anyway.
            # this creates a new xref
            form_ref = update_writer.add_object(form)
            root[pdf_name('/AcroForm')] = form_ref
            update_writer.update_root()
        # try to extend the existing form object first
        # and mark it for an update if necessary
        try:
            fields_ref = form.raw_get('/Fields')
            if isinstance(fields_ref, generic.IndirectObject):
                field_container_ref = fields_ref
                fields = fields_ref.get_object()
            else:
                fields = fields_ref
                # /Fields is directly embedded into form_ref, so that's
                # what we'll have to update if we create a new field
                field_container_ref = form_ref
        except KeyError:
            # shouldn't happen, but eh
            fields = generic.ArrayObject()
            field_container_ref = form_ref
            form[pdf_name('/Fields')] = fields

        candidates = enumerate_sig_fields_in(fields, with_name=sig_field_name)
        sig_field_ref = None
        try:
            field_name, value, sig_field_ref = next(candidates)
            if value is not None:
                raise ValueError(
                    'Signature field with name %s appears to be filled already.'
                    % sig_field_name
                )
        except StopIteration:
            if existing_fields_only:
                raise ValueError(
                    'No empty signature field with name %s found.'
                    % sig_field_name
                )
    except KeyError:
        # we have to create the form
        if existing_fields_only:
            raise ValueError('This file does not contain a form.')
        # no AcroForm present, so create one
        form = generic.DictionaryObject()
        root[pdf_name('/AcroForm')] = update_writer.add_object(form)
        fields = generic.ArrayObject()
        form[pdf_name('/Fields')] = fields
        # now we need to mark the root as updated
        update_writer.update_root()
        sig_field_ref = None

    field_created = sig_field_ref is None
    if field_created:
        # no signature field exists, so create one
        if existing_fields_only:
            raise ValueError('Could not find signature field')
        sig_form_kwargs = {
            'include_on_page': root['/Pages']['/Kids'][0],
        }
        sig_form_kwargs.update(**kwargs)
        sig_field = SignatureFormField(
            sig_field_name, writer=update_writer, **sig_form_kwargs
        )
        sig_field_ref = sig_field.reference
        fields.append(sig_field_ref)

        # make sure /SigFlags is present. If not, create it
        sig_flags = 3 if lock_sig_flags else 1
        form.setdefault(pdf_name('/SigFlags'), generic.NumberObject(sig_flags))
        # if we're adding a field to an existing form, this requires
        # registering an extra update
        if field_container_ref is not None:
            update_writer.mark_update(field_container_ref)

    return field_created, sig_field_ref


def enumerate_sig_fields(reader: PdfFileReader, filled_status=None):
    """
    Enumerate signature fields.

    :param reader:
        The PDF reader to operate on.
    :param filled_status:
        Optional boolean. If True (resp. False) then all filled (resp. empty)
        fields are returned. If left None (the default), then all fields
        are returned.
    :return:
        A generator producing signature fields.
    """

    root = reader.trailer['/Root']
    try:
        form = root['/AcroForm']
        fields = form['/Fields']
    except KeyError:
        return

    yield from enumerate_sig_fields_in(fields, filled_status)


def enumerate_sig_fields_in(field_list, filled_status=None, with_name=None):
    ft_sig = pdf_name('/Sig')
    for field_ref in field_list:
        # TODO the spec mandates this, but perhaps we should be a bit more
        #  tolerant
        assert isinstance(field_ref, generic.IndirectObject)
        field = field_ref.get_object()
        # /T is the field name. Required entry, but you never know.
        try:
            field_name = field['/T']
        except KeyError:
            continue
        field_type = field.get('/FT')
        if field_type != ft_sig:
            if with_name is not None and field_name == with_name:
                raise ValueError(
                    'Field with name %s exists but is not a signature field'
                    % field_name
                )
            continue
        field_value = field.get('/V')
        # "cast" to a regular string object
        filled = field_value is not None
        status_check = filled_status is None or filled == filled_status
        name_check = with_name is None or with_name == field_name
        if status_check and name_check:
            yield str(field_name), field_value, field_ref

        try:
            yield from enumerate_sig_fields_in(field['/Kids'])
        except KeyError:
            continue


def append_signature_fields(pdf_out: IncrementalPdfFileWriter, 
                            sig_field_specs: List[SigFieldSpec]):
    root = pdf_out.root

    page_list = root['/Pages']['/Kids']
    for sp in sig_field_specs:
        # use default appearance
        field_created, _ = _prepare_sig_field(
            sp.sig_field_name, root, update_writer=pdf_out,
            existing_fields_only=False, box=sp.box,
            include_on_page=page_list[sp.on_page]
        )
        if not field_created:
            raise ValueError(
                'Signature field with name %s already exists.'
                % sp.sig_field_name
            )

    output = BytesIO()
    pdf_out.write(output)
    output.seek(0)
    return output


SIG_DETAILS_DEFAULT_TEMPLATE = (
    'Digitally signed by %(signer)s.\n'
    'Timestamp: %(ts)s.'
)


class Timestamper:
    """
    Class to make RFC3161 timestamp requests
    """

    # see also
    # https://github.com/m32/endesive/blob/5e38809387b8bdb218d02cdcaa8f17b89a8a16fc/endesive/signer.py#L161

    def __init__(self, url, https=False, timeout=5):
        self.url = url
        self.https = https
        self.timeout = timeout

    def request_headers(self):
        return {'Content-Type': 'application/timestamp-query'}

    def get_nonce(self):
        # generate a random 8-byte unsigned integer
        return struct.unpack('=Q', os.urandom(8))[0]

    def request_cms(self, message_digest, md_algorithm):
        nonce = self.get_nonce()
        req = tsp.TimeStampReq({
            'version': 1,
            'message_imprint': tsp.MessageImprint({
                'hash_algorithm': algos.DigestAlgorithm({
                    'algorithm': md_algorithm
                }),
                'hashed_message': message_digest
            }),
            'nonce': nonce,
            # we want the server to send along its certs
            'cert_req': True
        })
        return nonce, req

    def timestamp(self, message_digest, md_algorithm):
        if self.https and not self.url.startswith('https://'):
            raise ValueError('Timestamp URL is not HTTPS.')
        nonce, req = self.request_cms(message_digest, md_algorithm)
        raw_res = requests.post(
            self.url, req.dump(), headers=self.request_headers(),
        )
        if raw_res.headers.get('Content-Type') != 'application/timestamp-reply':
            raise IOError('Timestamp server response is malformed.', raw_res)
        res = tsp.TimeStampResp.load(raw_res.content)
        pki_status_info = res['status']
        if pki_status_info['status'].native != 'granted':
            try:
                status_string = pki_status_info['status_string'].native
            except KeyError:
                status_string = ''
            try:
                fail_info = pki_status_info['fail_info'].native
            except KeyError:
                fail_info = ''
            raise IOError(
                f'Timestamp server refused our request: statusString '
                f'\"{status_string}\", failInfo \"{fail_info}\"'
            )
        tst = res['time_stamp_token']
        tst_info = tst['content']['encap_content_info']['content']
        nonce_received = tst_info.parsed['nonce'].native
        if nonce_received != nonce:
            raise IOError(
                f'Timestamp server sent back bad nonce value. Expected '
                f'{nonce}, but got {nonce_received}.'
            )
        return simple_cms_attribute('signature_time_stamp_token', tst)


class BasicAuthTimestamper(Timestamper):
    def __init__(self, url, username, password, https=True):
        super().__init__(url, https)
        self.username = username
        self.password = password

    def request_headers(self):
        h = super().request_headers()
        b64 = b64encode('%s:%s' % (self.username, self.password))
        h['Authorization'] = 'Basic ' + b64.decode('ascii')
        return h


class BearerAuthTimestamper(Timestamper):
    def __init__(self, url, token, https=True):
        super().__init__(url, https)
        self.token = token

    def request_headers(self):
        h = super().request_headers()
        h['Authorization'] = 'Bearer ' + self.token
        return h


def sign_pdf(pdf_out: IncrementalPdfFileWriter, 
             signature_meta: PdfSignatureMetadata, signer: Signer,
             existing_fields_only=False, bytes_reserved=None):

    # TODO generate an error when DocMDP doesn't allow extra signatures.

    # TODO explicitly disallow multiple certification signatures

    # TODO force md_algorithm to agree with the certification signature
    #  if present

    # TODO deal with SV dictionaries properly

    # TODO this function is becoming rather bloated, should refactor
    #  into a class for more fine-grained control

    root = pdf_out.root

    timestamp = datetime.now(tz=tzlocal.get_localzone())

    if bytes_reserved is None:
        test_md = getattr(hashlib, signature_meta.md_algorithm)().digest()
        test_signature = signer.sign(
            test_md, signature_meta.md_algorithm, timestamp=timestamp,
            dry_run=True
        ).hex().encode('ascii')
        bytes_reserved = len(test_signature)

    name = signature_meta.name
    if name is None:
        name = signer.subject_name
    # we need to add a signature object and a corresponding form field
    # to the PDF file
    # Here, we pass in the name as specified in the signature metadata.
    # When it's None, the reader will/should derive it from the contents
    # of the certificate.
    sig_obj = SignatureObject(
        timestamp, name=signature_meta.name, location=signature_meta.location, 
        reason=signature_meta.reason, bytes_reserved=bytes_reserved
    )
    sig_obj_ref = pdf_out.add_object(sig_obj)

    if signature_meta.field_name is None:
        if not existing_fields_only:
            raise ValueError('Not specifying a signature field name is only '
                             'allowed when existing_fields_only=True')

        # most of the logic in _prepare_sig_field has to do with preparing
        # for the potential addition of a new field. That is completely
        # irrelevant in this special case, so we might as well short circuit
        # things.
        field_created = False
        empty_fields = enumerate_sig_fields(pdf_out.prev, filled_status=False)
        try:
            field_name, _, sig_field_ref = next(empty_fields)
        except StopIteration:
            raise ValueError('There are no empty signature fields.')

        others = ', '.join(fn for fn, _, _ in empty_fields if fn is not None)
        if others:
            raise ValueError(
                'There are several empty signature fields. Please specify '
                'a field name. The options are %s, %s.' % (
                    field_name, others
                )
            )
    else:
        # grab or create a sig field
        field_created, sig_field_ref = _prepare_sig_field(
            signature_meta.field_name, root, update_writer=pdf_out,
            existing_fields_only=existing_fields_only, lock_sig_flags=True
        )
    sig_field = sig_field_ref.get_object()
    # fill in a reference to the (empty) signature object
    sig_field[pdf_name('/V')] = sig_obj_ref

    if not field_created:
        # still need to mark it for updating
        pdf_out.mark_update(sig_field_ref)

    x1, y1, x2, y2 = sig_field[pdf_name('/Rect')]
    w = abs(x1 - x2)
    h = abs(y1 - y2)
    if w and h:
        # the field is probably a visible one, so we change its appearance
        # stream to show some data about the signature
        # TODO allow customisation
        tss = TextStampStyle(
            stamp_text=SIG_DETAILS_DEFAULT_TEMPLATE,
            fixed_aspect_ratio=float(w/h)
        )
        text_params = {
            'signer': name, 'ts': timestamp.strftime(tss.timestamp_format)
        }
        stamp = TextStamp(pdf_out, tss, text_params=text_params)
        sig_field[pdf_name('/AP')] = stamp.as_appearances().as_pdf_object()
        try:
            # if there was an entry like this, it's meaningless now
            del sig_field[pdf_name('/AS')]
        except KeyError:
            pass

    if signature_meta.certify:
        _certification_setup(
            pdf_out, sig_obj_ref, signature_meta.md_algorithm,
            signature_meta.docmdp_permissions
        )

    # Render the PDF to a byte buffer with placeholder values
    # for the signature data
    output = BytesIO()
    pdf_out.write(output)

    # retcon time: write the proper values of the /ByteRange entry
    #  in the signature object
    eof = output.tell()
    sig_start, sig_end = sig_obj.signature_contents.offsets
    sig_obj.byte_range.fill_offsets(output, sig_start, sig_end, eof)

    # compute the digests
    output_buffer = output.getbuffer()
    md = getattr(hashlib, signature_meta.md_algorithm)()
    # these are memoryviews, so slices should not copy stuff around
    md.update(output_buffer[:sig_start])
    md.update(output_buffer[sig_end:])
    output_buffer.release()

    signature = signer.sign(
        md.digest(), signature_meta.md_algorithm, timestamp=timestamp
    ).hex().encode('ascii')

    assert len(signature) <= bytes_reserved

    # +1 to skip the '<'
    output.seek(sig_start + 1)
    output.write(signature)

    output.seek(0)
    return output