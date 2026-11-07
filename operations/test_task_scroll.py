from django.test import TestCase, override_settings
from django.urls import reverse

from .tests import CombinedServiceWorkflowTests


@override_settings(STORAGES={'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class TaskActionReturnsToCardTests(TestCase):
    setUp = CombinedServiceWorkflowTests.setUp
    create_visit = CombinedServiceWorkflowTests.create_visit

    def test_start_and_complete_return_to_the_pressed_card(self):
        visit, first, second = self.create_visit()
        first.tasks.filter(task_type__in=['HYGIENE', 'CONSULT']).update(status='COMPLETED')
        task = first.tasks.get(task_type='SERVICE')
        self.client.force_login(self.employee)
        page_url = reverse('execute_service', args=[first.pk])
        for action in ('start', 'complete'):
            response = self.client.post(reverse('task_action', args=[task.pk, action]))
            self.assertRedirects(response, f'{page_url}#task-{task.pk}', fetch_redirect_response=False)
        page = self.client.get(page_url)
        self.assertContains(page, f'id="task-{task.pk}"')

    def test_opening_confirm_returns_to_its_card(self):
        visit, first, second = self.create_visit()
        hygiene = first.tasks.get(task_type='HYGIENE')
        self.client.force_login(self.employee)
        response = self.client.post(reverse('task_action', args=[hygiene.pk, 'confirm']))
        self.assertRedirects(response, f"{reverse('execute_service', args=[first.pk])}#task-{hygiene.pk}",
                             fetch_redirect_response=False)

    def test_refused_action_stays_at_top_to_show_the_message(self):
        visit, first, second = self.create_visit()
        first.tasks.filter(task_type__in=['HYGIENE', 'CONSULT']).update(status='COMPLETED')
        task = first.tasks.get(task_type='SERVICE')
        self.client.force_login(self.employee)
        response = self.client.post(reverse('task_action', args=[task.pk, 'complete']))
        self.assertRedirects(response, reverse('execute_service', args=[first.pk]), fetch_redirect_response=False)
