import unittest
from unittest.mock import patch
from app import app, calculate_health_score
import pandas as pd

def test_quiz_navigation(client):
    # Test Step 1
    response = client.get('/quiz_step1')
    assert response.status_code == 200
    assert b'Question 1 of 10' in response.data

    # Test Step 1 to Step 2
    response = client.post('/quiz_step1', data={
        'question_1': 'No',
        'question_2': 'Yes',
        'question_3': 'No',
        'question_4': 'Yes',
        'language': 'en',
        'submit': 'Next'
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b'Question 5 of 10' in response.data

    # Test Step 2 Previous to Step 1
    response = client.post('/quiz_step2', data={
        'question_5': 'Yes',
        'question_6': 'No',
        'question_7': 'Yes',
        'language': 'en',
        'back': 'Back'
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b'Question 1 of 10' in response.data
    
class TestFicoreApp(unittest.TestCase):
    def setUp(self):
        app.testing = True
        self.client = app.test_client()

    def test_home_route(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'html', response.data)

    def test_submit_invalid_data(self):
        response = self.client.post('/submit', data={
            'business_name': '',
            'income_revenue': '-100',
            'expenses_costs': '100',
            'debt_loan': '0',
            'debt_interest_rate': '5',
            'auto_email': 'test@example.com',
            'email': 'test@example.com',
            'first_name': 'John'
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'Missing required field', response.data)

    def test_submit_email_mismatch(self):
        response = self.client.post('/submit', data={
            'business_name': 'Test Corp',
            'income_revenue': '1000',
            'expenses_costs': '500',
            'debt_loan': '200',
            'debt_interest_rate': '5',
            'auto_email': 'test1@example.com',
            'email': 'test2@example.com',
            'first_name': 'John'
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'Emails do not match', response.data)

    @patch('app.fetch_data_from_sheet')
    def test_dashboard_user_not_found(self, mock_fetch):
        mock_fetch.return_value = pd.DataFrame(columns=['Email', 'HealthScore'])
        response = self.client.get('/dashboard?email=test@example.com')
        self.assertEqual(response.status_code, 404)
        self.assertIn(b'User not found', response.data)

    def test_calculate_health_score(self):
        df = pd.DataFrame({
            'IncomeRevenue': [1000, 500],
            'ExpensesCosts': [500, 400],
            'DebtLoan': [200, 100],
            'DebtInterestRate': [5, 10]
        })
        result = calculate_health_score(df)
        self.assertIn('HealthScore', result.columns)
        self.assertTrue(all(0 <= score <= 100 for score in result['HealthScore']))
        self.assertIn('Badges', result.columns)
        self.assertTrue(isinstance(result['Badges'].iloc[0], str))

if __name__ == '__main__':
    unittest.main()

