# Testing Notes

| Test Case | Expected Result | Status |
|-----------|----------------|--------|
| Employee login with valid ID/pass | Redirect to dashboard | ✓ Pass |
| Employee login with wrong password | Show error message | ✓ Pass |
| Admin login with correct credentials | Redirect to admin panel | ✓ Pass |
| Admin login with wrong credentials | Show error message | ✓ Pass |
| Employee accesses /admin | Redirected to /dashboard | ✓ Pass |
| Clock in from allowed IP (DEMO_MODE) | Success, CSV row created | ✓ Pass |
| Clock in twice without clock out | Denied with message | ✓ Pass |
| Clock out without clock in | Denied with message | ✓ Pass |
| Clock in after 9:05 AM | Marked as Late | ✓ Pass |
| Weekly report generation | Groups by employee correctly | ✓ Pass |
| Missing CSV files | Auto-created on startup | ✓ Pass |
