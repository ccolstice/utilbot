const DRIVE_API = 'https://www.googleapis.com/drive/v3';
const DRIVE_UPLOAD_API = 'https://www.googleapis.com/upload/drive/v3';
const BACKUP_FOLDER_NAME = 'UtilBot Backups';
const BACKUP_PREFIX = 'utilbot-db-';

// Run this once from the Apps Script editor to request the narrow Drive scope.
function authorizeDrive() {
  backupFolderId_();
}

function doPost(e) {
  try {
    const input = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    const expected = PropertiesService.getScriptProperties().getProperty('SHARED_SECRET');
    if (!expected || !input.secret || input.secret !== expected) {
      return json_({ ok: false, error: 'unauthorized' });
    }

    if (input.action === 'upload') return upload_(input);
    if (input.action === 'list') return list_(input);
    if (input.action === 'download') return download_(input);
    return json_({ ok: false, error: 'unknown action' });
  } catch (error) {
    return json_({ ok: false, error: String(error && error.message ? error.message : error) });
  }
}

function upload_(input) {
  const name = String(input.name || '');
  if (!/^utilbot-db-\d{8}-\d{6}-\d{6}\.sqlite3$/.test(name)) {
    throw new Error('invalid backup filename');
  }
  if (!input.data || typeof input.data !== 'string') throw new Error('backup data is missing');

  const metadata = {
    name: name,
    mimeType: 'application/vnd.sqlite3',
    parents: [backupFolderId_()],
    description: 'UtilBot database backup; records=' + Number(input.itemCount || 0)
  };
  const created = driveRequest_('post', '/files?fields=id', metadata);
  const bytes = Utilities.base64Decode(input.data);
  try {
    driveRequest_('patch', '/files/' + encodeURIComponent(created.id) + '?uploadType=media', bytes,
      'application/vnd.sqlite3');
    const saved = driveRequest_('get', '/files/' + encodeURIComponent(created.id) +
      '?fields=id,name,size,createdTime,description');
    return json_({ ok: true, ...saved });
  } catch (error) {
    try { driveRequest_('delete', '/files/' + encodeURIComponent(created.id)); } catch (_) {}
    throw error;
  }
}

function list_(input) {
  const limit = Math.max(1, Math.min(Number(input.limit || 10), 50));
  const folderId = backupFolderId_();
  const query = "'" + folderId + "' in parents and name contains '" + BACKUP_PREFIX + "' and trashed = false";
  const params = '?q=' + encodeURIComponent(query) +
    '&orderBy=createdTime%20desc&pageSize=' + limit +
    '&fields=' + encodeURIComponent('files(id,name,size,createdTime,description)');
  const result = driveRequest_('get', '/files' + params);
  return json_({ ok: true, files: result.files || [] });
}

function download_(input) {
  const fileId = String(input.fileId || '');
  if (!fileId) throw new Error('fileId is missing');
  const metadata = driveRequest_('get', '/files/' + encodeURIComponent(fileId) + '?fields=id,name,parents');
  const folderId = backupFolderId_();
  if (!metadata.name || !metadata.name.startsWith(BACKUP_PREFIX) ||
      !(metadata.parents || []).includes(folderId)) {
    throw new Error('backup not found');
  }
  const bytes = driveRequest_('getBytes', '/files/' + encodeURIComponent(fileId) + '?alt=media');
  return json_({ ok: true, name: metadata.name, data: Utilities.base64Encode(bytes) });
}

function backupFolderId_() {
  const query = "name = '" + BACKUP_FOLDER_NAME + "' and mimeType = 'application/vnd.google-apps.folder' and trashed = false";
  const params = '?q=' + encodeURIComponent(query) + '&pageSize=1&fields=files(id)';
  const result = driveRequest_('get', '/files' + params);
  if (result.files && result.files.length) return result.files[0].id;
  const folder = driveRequest_('post', '/files?fields=id', {
    name: BACKUP_FOLDER_NAME,
    mimeType: 'application/vnd.google-apps.folder'
  });
  return folder.id;
}

function driveRequest_(method, path, body, contentType) {
  const options = {
    method: method === 'getBytes' ? 'get' : method,
    headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
    muteHttpExceptions: true
  };
  if (body !== undefined) {
    // UrlFetchApp encodes object payloads as form data even when contentType
    // is set to application/json. Serialize metadata explicitly as JSON.
    options.payload = contentType ? body : JSON.stringify(body);
    options.contentType = contentType || 'application/json';
  }
  const url = (method === 'getBytes' ? DRIVE_API :
    (method === 'patch' ? DRIVE_UPLOAD_API : DRIVE_API)) + path;
  const response = UrlFetchApp.fetch(url, options);
  const status = response.getResponseCode();
  if (status < 200 || status >= 300) {
    throw new Error('Drive API HTTP ' + status + ': ' + response.getContentText().slice(0, 500));
  }
  if (method === 'getBytes') return response.getContent();
  if (method === 'delete') return {};
  const text = response.getContentText();
  return text ? JSON.parse(text) : {};
}

function json_(value) {
  return ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}
