external_url 'https://Micahels-Mac-mini.local:8443'
nginx['listen_port'] = 443
nginx['ssl_certificate'] = '/etc/gitlab/tls/gitlab.crt'
nginx['ssl_certificate_key'] = '/etc/gitlab/tls/gitlab.key'
letsencrypt['enable'] = false
gitlab_rails['gitlab_shell_ssh_port'] = 2424
# GitLab's memory-constrained single-process Puma mode suits the 8GiB Docker VM.
puma['worker_processes'] = 0
sidekiq['concurrency'] = 10
