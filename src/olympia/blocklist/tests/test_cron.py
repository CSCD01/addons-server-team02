import datetime
import json
import os
from unittest import mock

from django.conf import settings
from django.core.files.storage import default_storage as storage

from freezegun import freeze_time
from waffle.testutils import override_switch

from olympia.amo.tests import addon_factory, TestCase, user_factory
from olympia.blocklist.cron import upload_mlbf_to_kinto
from olympia.blocklist.mlbf import MLBF
from olympia.blocklist.models import Block
from olympia.blocklist.tasks import MLBF_TIME_CONFIG_KEY
from olympia.lib.kinto import KintoServer
from olympia.zadmin.models import get_config, set_config


class TestUploadToKinto(TestCase):
    def setUp(self):
        addon_factory()
        self.block = Block.objects.create(
            addon=addon_factory(
                file_kw={'is_signed': True, 'is_webextension': True}),
            updated_by=user_factory())

    @freeze_time('2020-01-01 12:34:56')
    @override_switch('blocklist_mlbf_submit', active=True)
    @mock.patch.object(KintoServer, 'publish_attachment')
    def test_upload_mlbf_to_kinto(self, publish_mock):
        upload_mlbf_to_kinto()

        generation_time = int(
            datetime.datetime(2020, 1, 1, 12, 34, 56).timestamp() * 1000)
        publish_mock.assert_called_with(
            {'key_format': MLBF.KEY_FORMAT,
             'generation_time': generation_time},
            ('filter.bin', mock.ANY, 'application/octet-stream'))
        assert (
            get_config(MLBF_TIME_CONFIG_KEY, json_value=True) ==
            generation_time)

        mlfb_path = os.path.join(
            settings.MLBF_STORAGE_PATH, str(generation_time), 'filter')
        assert os.path.exists(mlfb_path)
        assert os.path.getsize(mlfb_path)

        blocked_path = os.path.join(
            settings.MLBF_STORAGE_PATH, str(generation_time), 'blocked.json')
        assert os.path.exists(blocked_path)
        assert os.path.getsize(blocked_path)

        not_blocked_path = os.path.join(
            settings.MLBF_STORAGE_PATH, str(generation_time),
            'notblocked.json')
        assert os.path.exists(not_blocked_path)
        assert os.path.getsize(not_blocked_path)

    @freeze_time('2020-01-01 12:34:56')
    @override_switch('blocklist_mlbf_submit', active=True)
    @mock.patch.object(KintoServer, 'publish_attachment')
    def test_stash_file(self, publish_mock):
        set_config(MLBF_TIME_CONFIG_KEY, 123456, json_value=True)
        prev_blocked_path = os.path.join(
            settings.MLBF_STORAGE_PATH, '123456', 'blocked.json')
        with storage.open(prev_blocked_path, 'w') as blocked_file:
            json.dump(['madeup@guid:123'], blocked_file)

        upload_mlbf_to_kinto()

        generation_time = int(
            datetime.datetime(2020, 1, 1, 12, 34, 56).timestamp() * 1000)

        stash_path = os.path.join(
            settings.MLBF_STORAGE_PATH, str(generation_time), 'stash.json')
        assert os.path.exists(stash_path)
        assert os.path.getsize(stash_path)
        with open(stash_path) as stash_file:
            blocked_guid = (
                f'{self.block.guid}:'
                f'{self.block.addon.current_version.version}')
            assert json.load(stash_file) == {
                'blocked': [blocked_guid],
                'unblocked': ['madeup@guid:123']}

    @override_switch('blocklist_mlbf_submit', active=False)
    @mock.patch.object(KintoServer, 'publish_attachment')
    def test_waffle_off_disables_publishing(self, publish_mock):
        upload_mlbf_to_kinto()

        publish_mock.assert_not_called()
        assert not get_config(MLBF_TIME_CONFIG_KEY)

    @freeze_time('2020-01-01 12:34:56')
    @override_switch('blocklist_mlbf_submit', active=True)
    @mock.patch.object(KintoServer, 'publish_attachment')
    def test_no_need_for_new_mlbf(self, publish_mock):
        # This was the last time the mlbf was generated
        last_time = int(
            datetime.datetime(2020, 1, 1, 12, 34, 1).timestamp() * 1000)
        # And the Block was modified just before so would be included
        self.block.update(modified=datetime.datetime(2020, 1, 1, 12, 34, 0))
        set_config(MLBF_TIME_CONFIG_KEY, last_time, json_value=True)
        upload_mlbf_to_kinto()
        # So no need for a new bloomfilter
        publish_mock.assert_not_called()

        # But if we add a new Block a new filter is needed
        addon_factory()
        Block.objects.create(
            addon=addon_factory(
                file_kw={'is_signed': True, 'is_webextension': True}),
            updated_by=user_factory())
        upload_mlbf_to_kinto()
        publish_mock.assert_called_once()
        assert (
            get_config(MLBF_TIME_CONFIG_KEY, json_value=True) ==
            int(datetime.datetime(2020, 1, 1, 12, 34, 56).timestamp() * 1000))
