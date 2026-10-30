-- Policy v5 for the Polaris log sidecar (ADR-081 Decision 3, docs/contracts/polaris-logs.md v3).
-- A port of AuditPolicy._decide and its helpers in images/polaris-log-batch/polaris_log_batch.py,
-- in Fluent Bit's Lua dialect (LuaJIT, Lua 5.1): one file, no require, no state, no clock.
-- Every record is decided on its own. Two decisions differ from the batch on purpose (ADR-081):
-- an access line with status 404 goes to the candidate index (rule 3'), and an application line
-- of rule 2b is kept without looking for its request's access line.
-- Fluent Bit calls polaris_filter(tag, timestamp, record) and takes -1 (drop) or
-- 2, timestamp, record (record modified, timestamp kept).

local ACCESS_LOGGER = "io.quarkus.http.access-log"
local COMMIT_LOGGER = "org.apache.polaris.service.catalog.iceberg.LocalIcebergCatalog"
local APP_ALLOW = {
  ["org.apache.polaris.service.exception.IcebergExceptionMapper"] = true,
  ["org.apache.polaris.service.admin.PolarisServiceImpl"] = true,
}
local KEEP_METHODS = { PUT = true, DELETE = true, PATCH = true }
local READ_METHODS = { GET = true, HEAD = true }
local TRIMMED = { "processName", "loggerClassName", "processId", "ndc" }
local POLICY = "policy-v5"

-- Quarkus's access pattern `%h %l %u %t "%r" %s %b`.
local ACCESS_PATTERN = '^(%S+) %S+ (%S+) %[[^%]]*%] "(%u+) (%S+)[^"]*" (%d+) (%S+)'

-- Order is priority: the first pattern that matches names the resource.
local RESOURCE_PATTERNS = {
  { "principal-role", "^.-/principal%-roles/[^/]+" },
  { "collection", "^.-/principal%-roles$" },
  { "catalog-role", "^.-/catalog%-roles/[^/]+" },
  { "collection", "^.-/catalog%-roles$" },
  { "auth", "^.-/oauth/tokens$" },
  { "config", "^.-/v1/config$" },
  { "table", "^.-/tables/rename$" },
  { "view", "^.-/views/rename$" },
  { "transaction", "^.-/transactions/commit$" },
  { "collection", "^.-/namespaces/[^/]+/register$" },
  { "namespace", "^.-/namespaces/[^/]+/properties$" },
  { "table", "^.-/namespaces/[^/]+/tables/[^/]+" },
  { "view", "^.-/namespaces/[^/]+/views/[^/]+" },
  { "collection", "^.-/namespaces/[^/]+/tables$" },
  { "collection", "^.-/namespaces/[^/]+/views$" },
  { "namespace", "^.-/namespaces/[^/]+$" },
  { "collection", "^.-/namespaces$" },
  { "catalog", "^.-/catalogs/[^/]+" },
  { "collection", "^.-/catalogs$" },
  { "principal", "^.-/principals/[^/]+" },
  { "collection", "^.-/principals$" },
}

local function path_only(p)
  if type(p) ~= "string" then
    return ""
  end
  return (p:match("^([^?]*)"))
end

local function api_of(path)
  if path:sub(1, 16) == "/api/management/" then
    return "management"
  end
  if path:sub(1, 13) == "/api/catalog/" then
    return "catalog"
  end
  return "other"
end

local function classify_path(path)
  for _, entry in ipairs(RESOURCE_PATTERNS) do
    local hit = path:match(entry[2])
    if hit then
      return hit, entry[1]
    end
  end
  return path, "other"
end

-- A clientSecret that is not Polaris's own mask becomes <redacted>.
local function redact_secret(rec)
  local msg = rec.message
  if type(msg) ~= "string" or not msg:find("clientSecret", 1, true) then
    return
  end
  local hit = false
  local out = msg:gsub("(clientSecret:%s*)(%S+)", function(head, value)
    if value == "*" or value == "<redacted>" then
      return nil
    end
    hit = true
    return head .. "<redacted>"
  end)
  if hit then
    rec.message = out
    rec.secret_redacted = true
  end
end

-- Adds client_ip .. response_size to an access record; false when the pattern did not match.
local function parse_access(rec)
  local msg = rec.message
  local ip, user, method, path, status, size
  if type(msg) == "string" then
    ip, user, method, path, status, size = msg:match(ACCESS_PATTERN)
  end
  if not ip then
    rec.access_log_parse_error = true
    return false
  end
  rec.client_ip, rec.user_principal_name = ip, user
  rec.http_method, rec.api_path = method, path
  rec.http_status = tonumber(status)
  rec.response_size = size:match("^%d+$") and tonumber(size) or 0 -- %b writes "-" for 0 bytes
  return true
end

local function status_class(status)
  if status < 400 then
    return "ok"
  end
  if status == 401 or status == 403 or status == 404 then
    return tostring(status)
  end
  if status < 500 then
    return "4xx"
  end
  return "5xx"
end

local function text(v)
  if type(v) == "string" and v ~= "" then
    return v
  end
  return "-"
end

-- <hostName>:<timestamp>:<sequence>, the sequence written with %d so that it stays an integer.
local function event_id(rec)
  local seq = "-"
  if type(rec.sequence) == "number" then
    seq = string.format("%d", rec.sequence)
  end
  return text(rec.hostName) .. ":" .. text(rec.timestamp) .. ":" .. seq
end

-- Returns the rule and whether the line is kept.
local function decide(rec, is_access, parsed)
  local level, logger = rec.level, rec.loggerName
  if level == "ERROR" or level == "WARN" then
    return "1", "error"
  end
  if not is_access then
    if logger == COMMIT_LOGGER then
      return "2a", nil
    end
    if APP_ALLOW[logger] then
      return "2b", "candidate"
    end
    return "2c", nil
  end
  local status, method = rec.http_status, rec.http_method
  if status == 404 then
    return "3'", "candidate"
  end
  if not parsed or status >= 400 then
    return "3", "error"
  end
  if KEEP_METHODS[method] then
    return "4", "candidate"
  end
  if method == "POST" then
    if path_only(rec.api_path):sub(1, 16) == "/api/management/" then
      return "5", "candidate"
    end
    return "-", nil
  end
  if READ_METHODS[method] then
    return "6", nil
  end
  return "7", "error"
end

function polaris_filter(tag, timestamp, record)
  redact_secret(record)
  local is_access = record.loggerName == ACCESS_LOGGER
  local parsed = is_access and parse_access(record)
  local rule, index = decide(record, is_access, parsed)
  if not index then
    return -1
  end

  local resource, kind, api, class = "-", "none", "none", "none"
  if parsed then
    local path = path_only(record.api_path)
    resource, kind = classify_path(path)
    api = api_of(path)
    class = status_class(record.http_status)
  elseif is_access then
    class = "unparsed"
  end

  record.user_principal_name = record.user_principal_name or "-"
  record.event_id = event_id(record)
  record.index_class = index
  record.policy_rule = rule
  record.policy = POLICY
  record.resource = resource
  record.resource_kind = kind
  record.api_kind = api
  record.status_class = class
  for _, field in ipairs(TRIMMED) do
    record[field] = nil
  end
  return 2, timestamp, record
end
