# Run once inside the newly installed GitLab container. Never prints credentials.
require 'json'
require 'securerandom'
root=User.find_by_username('root')
raise 'Root account missing' unless root
agent=User.find_by_username('biome-agent')
unless agent
 password=SecureRandom.base64(36)
 result=Users::CreateService.new(root,{username:'biome-agent',name:'Biome coding agents',email:'biome-agent@biome.local',password:password,password_confirmation:password,skip_confirmation:true,organization_id:root.organization_id}).execute
 raise result.message unless result.success?
 agent=result.payload[:user]
end
scope='mcp api read_repository write_repository'
application=Doorkeeper::Application.create!(name:'Biome Git capability proxy',redirect_uri:'https://127.0.0.1/biome-gitlab-callback',scopes:scope,confidential:true,owner:root)
access=Doorkeeper::AccessToken.create!(application:application,resource_owner_id:agent.id,scopes:scope,expires_in:7200,use_refresh_token:true)
client_secret=application.respond_to?(:plaintext_secret) ? application.plaintext_secret : application.secret
client_secret ||= application.secret
access_value=access.respond_to?(:plaintext_token) ? access.plaintext_token : access.token
access_value ||= access.token
refresh_value=access.respond_to?(:plaintext_refresh_token) ? access.plaintext_refresh_token : access.refresh_token
refresh_value ||= access.refresh_token
raise 'OAuth material missing' if [client_secret,access_value,refresh_value].any?(&:blank?)
settings=ApplicationSetting.current
settings.update!(signup_enabled:false)
File.write('/var/opt/gitlab/biome-bootstrap/biome-oauth.json',JSON.generate({client_id:application.uid,client_secret:client_secret,access_token:access_value,refresh_token:refresh_value,expires_at:Time.now.to_f+7200}))
File.chmod(0600,'/var/opt/gitlab/biome-bootstrap/biome-oauth.json')
initial=File.read('/var/opt/gitlab/biome-bootstrap/initial_root_password')
password=initial.lines.find{|l|l.start_with?('Password: ')}.to_s.sub('Password: ','').strip
File.write('/var/opt/gitlab/biome-bootstrap/biome-admin-access.json',JSON.generate({url:'https://Micahels-Mac-mini.local:8443',username:'root',password:password}))
File.chmod(0600,'/var/opt/gitlab/biome-bootstrap/biome-admin-access.json')
puts JSON.generate({gitlab:Gitlab::VERSION,agent:agent.username,oauth_scopes:scope,mcp_enabled:settings.respond_to?(:mcp_server_enabled) ? settings.mcp_server_enabled : 'see settings'})
