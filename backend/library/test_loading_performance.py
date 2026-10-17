from runtime.testing import force_credential_login
"""Load-path invariants: bounded task materialization and exact folder totals."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from library import catalog, queue
from library.models import Bookmark, Folder, Job, JobGroup, LibrarySettings
from runtime.models import RuntimeState


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class LoadingPerformanceTests(TestCase):
    databases = {'default', 'library'}

    def setUp(self):
        RuntimeState.objects.create(pk=1)
        LibrarySettings.objects.create(pk=1)
        owner = get_user_model().objects.create_user('owner')
        force_credential_login(self.client, owner)

    def test_folder_totals_include_descendants_without_repeated_subtree_scans(self):
        root = catalog.create_folder({'name':'开发'})
        child = catalog.create_folder({'name':'文档','parent_id':str(root.id)})
        leaf = catalog.create_folder({'name':'API','parent_id':str(child.id)})
        empty = catalog.create_folder({'name':'空目录','parent_id':str(root.id)})
        other = catalog.create_folder({'name':'设计'})
        for index, folder in enumerate([root, child, leaf, leaf, other, None]):
            catalog.create_bookmark({'url':f'https://example.org/{index}',
                                     'snapshot_enabled':False, 'folder_id':str(folder.id) if folder else None})
        # The API must no longer ask for every folder's entire subtree.
        with patch('library.catalog.descendant_ids', side_effect=AssertionError('repeated subtree scan')):
            response = self.client.get('/api/v1/folders')
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual({r['id']:r['count'] for r in response.json()['data']},
            {str(root.id):4,str(child.id):3,str(leaf.id):2,str(empty.id):0,str(other.id):1})
        Bookmark.objects.filter(folder=leaf).delete()
        totals={r['id']:r['count'] for r in self.client.get('/api/v1/folders').json()['data']}
        self.assertEqual((totals[str(root.id)],totals[str(child.id)],totals[str(leaf.id)]),(2,1,0))

    def test_task_page_materializes_only_page_and_current_group(self):
        groups=JobGroup.objects.bulk_create([JobGroup(kind='classification') for _ in range(125)])
        active=groups[0]
        queue.enqueue('classification',group=active)
        loaded=[]
        original=JobGroup.from_db
        def observe(*args, **kwargs):
            obj=original(*args,**kwargs)
            loaded.append(obj.id)
            return obj
        with patch.object(JobGroup,'from_db',side_effect=observe):
            response=self.client.get('/api/v1/job-groups?page=2&page_size=2')
        self.assertEqual(response.status_code,200,response.content)
        body=response.json()
        self.assertEqual(body['page'],{'number':2,'size':2,'total':125})
        self.assertEqual(len(body['data']['groups']),2)
        self.assertEqual(body['data']['current_group']['id'],str(active.id))
        self.assertLessEqual(len(loaded),3,'history pagination must not construct all JobGroup objects')

    def test_task_paging_preserves_every_state_order_and_summary(self):
        now=timezone.now()
        groups=[]
        cases=[[],['succeeded'],['failed'],['cancelled'],['succeeded','failed'],
               ['retry_wait'],['queued','retry_wait'],['running','queued']]
        expected={}
        for offset, states in enumerate(cases):
            group=JobGroup.objects.create(kind='refresh',requested_count=len(states))
            JobGroup.objects.filter(pk=group.pk).update(created_at=now+timedelta(seconds=offset))
            for state in states:
                job=queue.enqueue('capture',group=group)
                Job.objects.filter(pk=job.pk).update(state=state)
            expected[str(group.id)]=queue.group_status(group)
            groups.append(group)
        actual=[]
        for page in range(1,5):
            body=self.client.get(f'/api/v1/job-groups?page={page}&page_size=2').json()
            self.assertEqual(body['data']['active_count'],3)
            self.assertEqual(body['data']['current_group']['id'],str(groups[-1].id))
            for row in body['data']['groups']:
                actual.append(row['id'])
                reference=expected[row['id']]
                for key in ('state','total','completed','counts'):
                    self.assertEqual(row[key],reference[key])
        self.assertEqual(actual,[str(g.id) for g in reversed(groups)])
        page=self.client.get('/api/v1/job-groups?page=100&page_size=2').json()
        self.assertEqual(page['data']['groups'],[])
        self.assertEqual(page['data']['current_group']['id'],str(groups[-1].id))
