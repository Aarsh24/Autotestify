import requests
from github import Github
import os
from datetime import datetime
import base64
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

class GitHubService:
    def __init__(self):
        self.token = os.environ.get('GITHUB_TOKEN')
        if self.token:
            try:
                self.github = Github(self.token, per_page=100)
                # Test the token immediately
                self.github.get_user().login
            except Exception as e:
                print(f"Invalid GitHub token: {e}")
                # Fall back to unauthenticated access
                self.github = Github(per_page=30)
                self.token = None
        else:
            self.github = Github(per_page=30)

        self.last_request_time = 0
        self.min_request_interval = 1.0
        self.driver_path = './chromedriver.exe'

    def _rate_limit_check(self):
        current_time = time.time()
        time_since_last = current_time - self.last_request_time
        if time_since_last < self.min_request_interval:
            time.sleep(self.min_request_interval - time_since_last)
        self.last_request_time = time.time()

    def _check_rate_limit_status(self):
        try:
            rate_limit = self.github.get_rate_limit()
            core_remaining = rate_limit.core.remaining
            core_reset_time = rate_limit.core.reset

            if core_remaining < 10:
                wait_time = max(0, core_reset_time.timestamp() - time.time())
                if wait_time > 0:
                    time.sleep(min(wait_time, 300))

            return core_remaining > 0
        except Exception:
            return True

    def analyze_repository(self, owner, repo_name):
        try:
            if self.token and not self._check_rate_limit_status():
                raise Exception("GitHub API rate limit exceeded.")

            self._rate_limit_check()
            try:
                repo = self.github.get_repo(f"{owner}/{repo_name}")
            except Exception as e:
                if "401" in str(e) or "Bad credentials" in str(e):
                    # Try without authentication
                    self.github = Github(per_page=30)
                    self.token = None
                    repo = self.github.get_repo(f"{owner}/{repo_name}")
                else:
                    raise e

            repo_data = {
                'name': repo.name,
                'full_name': repo.full_name,
                'description': repo.description,
                'url': repo.html_url,
                'language': repo.language,
                'stars': repo.stargazers_count,
                'forks': repo.forks_count,
                'created_at': repo.created_at,
                'updated_at': repo.updated_at,
                'size': repo.size,
                'branches': [],
                'commits': [],
                'files': [],
                'contributors': [],
                'ui_validation': {},
                'key_files': {}
            }

            self._rate_limit_check()
            branches = list(repo.get_branches()[:10])
            for branch in branches:
                repo_data['branches'].append({
                    'name': branch.name,
                    'protected': branch.protected,
                    'commit_sha': branch.commit.sha
                })

            self._rate_limit_check()
            commits = list(repo.get_commits()[:20])
            for commit in commits:
                try:
                    repo_data['commits'].append({
                        'sha': commit.sha,
                        'message': commit.commit.message,
                        'author': commit.commit.author.name,
                        'date': commit.commit.author.date,
                        'additions': commit.stats.additions if commit.stats else 0,
                        'deletions': commit.stats.deletions if commit.stats else 0
                    })
                except Exception:
                    continue

            try:
                self._rate_limit_check()
                contents = repo.get_contents("")
                self._get_repository_files(repo, contents, repo_data['files'])
            except Exception:
                pass

            try:
                self._rate_limit_check()
                contributors = list(repo.get_contributors()[:5])
                for contributor in contributors:
                    repo_data['contributors'].append({
                        'login': contributor.login,
                        'contributions': contributor.contributions,
                        'avatar_url': contributor.avatar_url
                    })
            except Exception:
                pass

            repo_data['key_files'] = self._check_key_files_presence(repo_data['files'])
            repo_data['ui_validation'] = self._validate_github_ui_with_selenium(owner, repo_name)

            return repo_data

        except Exception as e:
            raise Exception(f"Error analyzing repository: {str(e)}")

    def _check_key_files_presence(self, files):
        expected = ['Dockerfile', '.github/workflows', 'README.md', '.gitignore']
        paths = [f['path'] for f in files]
        return {key: any(key in path for path in paths) for key in expected}
    
    
    def _validate_github_ui_with_selenium(self, owner, repo_name):
        result = {
            'readme_found': False,
            'badge_found': False,
            'actions_visible': False,
            'screenshot': None
        }

        try:
            options = Options()
            options.add_argument('--headless')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            driver = webdriver.Chrome(options=options)

            url = f"https://github.com/{owner}/{repo_name}"
            driver.get(url)

            time.sleep(2)
            folder = "screenshots"  # NOT static/screenshots
            os.makedirs(folder, exist_ok=True)

            filename = f"{owner}_{repo_name}_screenshot.png"
            screenshot_path = os.path.join(folder, filename)

            driver.save_screenshot(screenshot_path)

            # JSON return
            result['screenshot'] = screenshot_path            # full path if needed
            result['screenshot_path'] = filename 

            # try:
            #     readme = driver.find_element(By.XPATH, "//*[contains(@id, 'readme')]")
            #     if readme:
            #         result['readme_found'] = True
            try:
                files = WebDriverWait(driver, 10).until(
                    EC.presence_of_all_elements_located((By.CSS_SELECTOR, '.react-directory-filename-column a'))
                )
                for file in files:
                    if file.text.strip().lower() == "readme.md":
                        result["readme_found"] = True
                        break
            except TimeoutException:
                result["readme_found"] = False

            try:
                driver.get(f"https://github.com/{owner}/{repo_name}/actions")
                WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.TAG_NAME, "main"))
                )
                result['actions_visible'] = True
            except TimeoutException:
                result['actions_visible'] = False

            driver.quit()

        except Exception as e:
            print(f"Selenium error: {e}")

        return result

    def _get_repository_files(self, repo, contents, files_list, path="", max_files=None):
        for content in contents:
            if max_files is not None and len(files_list) >= max_files:
                return

            if content.type == "dir":
                try:
                    self._rate_limit_check()
                    subcontents = repo.get_contents(content.path)
                    self._get_repository_files(repo, subcontents, files_list, content.path, max_files)
                except Exception:
                    continue
            else:
                if self._is_code_file(content.name):
                    try:
                        file_content = ""
                        if content.size < 50000:
                            self._rate_limit_check()
                            file_content = base64.b64decode(content.content).decode('utf-8', errors='ignore')

                        files_list.append({
                            'name': content.name,
                            'path': content.path,
                            'size': content.size,
                            'type': self._get_file_type(content.name),
                            'content': file_content,
                            'url': content.html_url
                        })
                    except Exception:
                        continue

    def _is_code_file(self, filename):
        code_extensions = [
            '.py', '.js', '.html', '.css', '.java', '.cpp', '.c', '.h',
            '.php', '.rb', '.go', '.rs', '.ts', '.jsx', '.tsx', '.vue',
            '.xml', '.json', '.yml', '.yaml', '.md', '.sql'
        ]
        return any(filename.lower().endswith(ext) for ext in code_extensions)

    def _get_file_type(self, filename):
        ext = filename.lower().split('.')[-1] if '.' in filename else 'unknown'
        type_mapping = {
            'py': 'Python', 'js': 'JavaScript', 'ts': 'TypeScript', 'jsx': 'React', 'tsx': 'TypeScript React',
            'html': 'HTML', 'css': 'CSS', 'java': 'Java', 'cpp': 'C++', 'c': 'C', 'php': 'PHP', 'rb': 'Ruby',
            'go': 'Go', 'rs': 'Rust', 'vue': 'Vue.js', 'xml': 'XML', 'json': 'JSON', 'yml': 'YAML',
            'yaml': 'YAML', 'md': 'Markdown', 'sql': 'SQL'
        }
        return type_mapping.get(ext, ext.upper())


# ---------------- Test Cases ------------------
import unittest
from unittest.mock import patch, MagicMock

class TestGitHubService(unittest.TestCase):

    @patch('selenium.webdriver.Chrome')
    def test_validate_github_ui_with_selenium_success(self, mock_webdriver):
        service = GitHubService()
        mock_driver = MagicMock()
        mock_webdriver.return_value = mock_driver

        mock_driver.find_element.return_value = True
        mock_driver.find_elements.return_value = [MagicMock()]
        mock_driver.save_screenshot.return_value = True

        result = service._validate_github_ui_with_selenium('octocat', 'Hello-World')
        self.assertTrue(result['readme_found'])
        self.assertTrue(result['badge_found'])
        self.assertIn('screenshots/', result['screenshot'])

    @patch('selenium.webdriver.Chrome')
    def test_validate_github_ui_with_selenium_timeout(self, mock_webdriver):
        service = GitHubService()
        mock_driver = MagicMock()
        mock_webdriver.return_value = mock_driver

        def raise_timeout(*args, **kwargs):
            raise TimeoutException("Timeout")

        mock_driver.find_element.side_effect = Exception("Not found")
        mock_driver.find_elements.return_value = []
        mock_driver.get.side_effect = raise_timeout

        result = service._validate_github_ui_with_selenium('octocat', 'Hello-World')
        self.assertFalse(result['actions_visible'])

if __name__ == '__main__':
    unittest.main()

