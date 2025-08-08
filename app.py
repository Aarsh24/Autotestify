from flask import Flask, render_template, request, jsonify, send_file, send_from_directory
import os
from datetime import datetime
from services.github_service import GitHubService
from services.gemini_service import GeminiService 
from services.api_tester import APITester
from services.email_service import EmailService
from services.report_generator import ReportGenerator
from config import Config
import json

app = Flask(__name__)
app.config.from_object(Config)

# Initialize services
github_service = GitHubService()
gemini_service = GeminiService()
api_tester = APITester()
report_generator = ReportGenerator()
email_service = EmailService(app.config)

# ─────────────────────────────── ROUTES ───────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/github-analysis')
def github_analysis():
    return render_template('github_analysis.html')

@app.route('/api-testing')
def api_testing():
    return render_template('api_testing.html')

@app.route('/reports')
def reports():
    reports_dir = 'reports'
    os.makedirs(reports_dir, exist_ok=True)
    report_files = []

    for filename in os.listdir(reports_dir):
        if filename.endswith('.html'):
            filepath = os.path.join(reports_dir, filename)
            report_info = {
                'filename': filename,
                'timestamp': datetime.fromtimestamp(os.path.getctime(filepath)),
                'size': os.path.getsize(filepath),
                'selenium_ui': None
            }

            # Try to load matching JSON metadata file
            json_filename = filename.replace('.html', '.json')
            json_path = os.path.join(reports_dir, json_filename)
            if os.path.exists(json_path):
                try:
                    with open(json_path, 'r') as f:
                        metadata = json.load(f)
                        if 'selenium_ui' in metadata:
                            report_info['selenium_ui'] = metadata['selenium_ui']
                except Exception as e:
                    print(f"Error reading metadata for {filename}: {e}")

            report_files.append(report_info)

    report_files.sort(key=lambda x: x['timestamp'], reverse=True)
    return render_template('reports.html', reports=report_files)


# ─────────────────────────────── API ROUTES ───────────────────────────────

@app.route('/screenshots/<path:filename>')
def screenshots(filename):
    return send_from_directory('screenshots', filename)

@app.route('/api/analyze-github', methods=['POST'])
def analyze_github():
    try:
        data = request.get_json()
        repo_url = data.get('repo_url')
        email = data.get('email', '')

        if not repo_url:
            return jsonify({'error': 'Repository URL is required'}), 400

        parts = repo_url.replace('https://github.com/', '').split('/')
        if len(parts) < 2:
            return jsonify({'error': 'Invalid GitHub URL format'}), 400

        owner, repo_name = parts[0], parts[1]

        # Step 1: Analyze repo
        repo_data = github_service.analyze_repository(owner, repo_name)
        code_assessment = gemini_service.assess_code_quality(repo_data['files'])

        # Step 2: Run Selenium UI Checks
        selenium_results = github_service._validate_github_ui_with_selenium(owner, repo_name)
        screenshot_path = selenium_results.get("screenshot")
        
        selenium_ui_data = {
            **selenium_results,
            'screenshot_path': os.path.basename(screenshot_path) if screenshot_path else None
        }

        # Step 3: Generate HTML report
        report_filename = f"github_analysis_{owner}_{repo_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        report_path = report_generator.generate_github_report(
            repo_data=repo_data,
            code_assessment=code_assessment,
            filename=report_filename,
            selenium_ui_data=selenium_ui_data
        )

        # Step 4: Email report (if requested)
        if email:
            email_service.send_report_email(email, report_path, 'GitHub Analysis Report')

        return jsonify({
            'success': True,
            'report_url': f'/download-report/{report_filename}',
            'message': 'Analysis completed successfully!'
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        
        error_message = str(e)
        if "401" in error_message or "Bad credentials" in error_message:
            error_message = "GitHub authentication failed. Please check your GitHub token in the .env file or the repository may be private."
        elif "403" in error_message:
            error_message = "GitHub API rate limit exceeded. Please wait a few minutes and try again."
        elif "404" in error_message:
            error_message = "Repository not found. Please check the repository URL."
        
        return jsonify({'success': False, 'error': error_message}), 500

@app.route('/api/test-api', methods=['POST'])
def test_api():
    try:
        data = request.get_json()
        base_url = data.get('base_url')
        email = data.get('email', '')
        endpoints = data.get('endpoints', [])

        if not base_url:
            return jsonify({'error': 'Base URL is required'}), 400

        test_results = api_tester.test_endpoints(base_url, endpoints)

        report_filename = f"api_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        report_path = report_generator.generate_api_report(base_url, test_results, report_filename)

        if email:
            email_service.send_report_email(email, report_path, 'API Testing Report')

        return jsonify({
            'success': True,
            'report_url': f'/download-report/{report_filename}',
            'message': 'API testing completed successfully!'
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ──────────────────────── GitHub UI Check (Selenium) ────────────────────────

@app.route('/api/github-ui-check', methods=['POST'])
def github_ui_check():
    try:
        data = request.get_json()
        repo_url = data.get('repo_url')

        if not repo_url:
            return jsonify({'error': 'Repository URL is required'}), 400

        check_result = github_service.perform_ui_checks(repo_url)
        return jsonify({'success': True, 'results': check_result})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/github-screenshot', methods=['POST'])
def github_screenshot():
    try:
        data = request.get_json()
        repo_url = data.get('repo_url')

        if not repo_url:
            return jsonify({'error': 'Repository URL is required'}), 400

        screenshot_path = github_service.capture_repo_screenshot(repo_url)

        if os.path.exists(screenshot_path):
            return send_file(screenshot_path, mimetype='image/png')

        return jsonify({'error': 'Screenshot failed'}), 500

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ─────────────────────────────── UTIL ROUTES ───────────────────────────────

@app.route('/download-report/<filename>')
def download_report(filename):
    try:
        report_path = os.path.join('reports', filename)
        if os.path.exists(report_path):
            return send_file(report_path, as_attachment=True)
        return jsonify({'error': 'Report not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/view-report/<filename>')
def view_report(filename):
    try:
        report_path = os.path.join('reports', filename)
        if os.path.exists(report_path):
            return send_file(report_path)
        return jsonify({'error': 'Report not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─────────────────────────────── BOOTSTRAP ───────────────────────────────

if __name__ == '__main__':
    os.makedirs('reports', exist_ok=True)
    os.makedirs('static/charts', exist_ok=True)
    os.makedirs('screenshots', exist_ok=True)

    app.run(debug=True, host='0.0.0.0', port=5000)

