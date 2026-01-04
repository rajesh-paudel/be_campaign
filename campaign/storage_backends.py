from storages.backends.s3boto3 import S3Boto3Storage



class PublicMediaStorage(S3Boto3Storage):
    location = 'public'
    default_acl = 'public-read'
    file_overwrite = False
    querystring_auth = False  # This is important for public access
    signature_version = 's3v4'  # Use the latest signature version

    @property
    def querystring_auth(self):
        return False
