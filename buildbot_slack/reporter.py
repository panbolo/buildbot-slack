# Based on the gitlab reporter from buildbot

from __future__ import absolute_import, print_function

from twisted.internet import defer

from buildbot.process.properties import Properties
from buildbot.process.results import statusToString
from buildbot.process import results
from buildbot.reporters import http, utils
from buildbot.util import httpclientservice
from buildbot.util.logger import Logger

logger = Logger()

STATUS_EMOJIS = {
    "success": ":sunglassses:",
    "warnings": ":meow_wow:",
    "failure": ":skull:",
    "skipped": ":slam:",
    "exception": ":skull:",
    "retry": ":facepalm:",
    "cancelled": ":slam:",
}
STATUS_COLORS = {
    "success": "#36a64f",
    "warnings": "#fc8c03",
    "failure": "#fc0303",
    "skipped": "#fc8c03",
    "exception": "#fc0303",
    "retry": "#fc8c03",
    "cancelled": "#fc8c03",
}
DEFAULT_HOST = "https://hooks.slack.com"  # deprecated

def getValueOrDefault(key, default, **kwargs):
    if key in kwargs:
        return kwargs[key]
    else:
        return default

def isSuccess(status):
    return status != None and status == results.SUCCESS

def isFailure(status):
    return status != None and (status == results.FAILURE or status == results.EXCEPTION)

class SlackStatusPush(http.HttpStatusPushBase):
    name = "SlackStatusPush"
    neededDetails = dict(wantProperties=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reportBuildStated = getValueOrDefault("reportBuildStated", True, **kwargs)
        self.reportOnlyFailures = getValueOrDefault("reportOnlyFailures", False, **kwargs)
        self.reportFixedBuild = getValueOrDefault("reportFixedBuild", False, **kwargs)
        self.verify = getValueOrDefault("verify", False, **kwargs)
        self.prevBuildResults = {}

    def checkConfig(
        self, endpoint, channel=None, host_url=None, username=None, **kwargs
    ):
        if not isinstance(endpoint, str):
            logger.warning(
                "[SlackStatusPush] endpoint should be a string, got '%s' instead",
                type(endpoint).__name__,
            )
        elif not endpoint.startswith("http"):
            logger.warning(
                '[SlackStatusPush] endpoint should start with "http...", endpoint: %s',
                endpoint,
            )
        if channel and not isinstance(channel, str):
            logger.warning(
                "[SlackStatusPush] channel must be a string, got '%s' instead",
                type(channel).__name__,
            )
        if username and not isinstance(username, str):
            logger.warning(
                "[SlackStatusPush] username must be a string, got '%s' instead",
                type(username).__name__,
            )
        if host_url and not isinstance(host_url, str):  # deprecated
            logger.warning(
                "[SlackStatusPush] host_url must be a string, got '%s' instead",
                type(host_url).__name__,
            )
        elif host_url:
            logger.warning(
                "[SlackStatusPush] argument host_url is deprecated and will be removed in the next release: specify the full url as endpoint"
            )

    @defer.inlineCallbacks
    def reconfigService(
        self,
        endpoint,
        channel=None,
        host_url=None,  # deprecated
        username=None,
        attachments=True,
        verbose=False,
        **kwargs
    ):

        yield super().reconfigService(**kwargs)

        self.baseUrl = host_url and host_url.rstrip("/")  # deprecated
        if host_url:
            logger.warning(
                "[SlackStatusPush] argument host_url is deprecated and will be removed in the next release: specify the full url as endpoint"
            )
        self.endpoint = endpoint
        self.channel = channel
        self.username = username
        self.attachments = attachments
        self._http = yield httpclientservice.HTTPClientService.getService(
            self.master,
            self.baseUrl or self.endpoint,
            debug=self.debug,
            verify=self.verify,
        )
        self.verbose = verbose
        self.project_ids = {}

    @defer.inlineCallbacks
    def getAttachments(self, build, key):
        sourcestamps = build["buildset"]["sourcestamps"]
        attachments = []

        for sourcestamp in sourcestamps:
            title = "<{url}|Build #{buildid}> - *{status}*".format(
                url=build["url"], buildid=build["buildid"], status=statusToString(build["results"])
            )
            
            blocks = []
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": title
                    }
                }
            )

            if build["results"] != results.SUCCESS:
                responsible_users = yield utils.getResponsibleUsersForBuild(self.master, build["buildid"])
                if responsible_users:
                    commiters = "*Commiters:*\n{}".format(", ".join(responsible_users))
                    blocks.append(
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": commiters
                            }
                        }
                    )

            attachments.append(
                {
                    "color": STATUS_COLORS.get(statusToString(build["results"]), ""),
                    "blocks": blocks,
                }
            )
        return attachments

    @defer.inlineCallbacks
    def getBuildDetailsAndSendMessage(self, build, key):
        yield utils.getDetailsForBuild(self.master, build, **self.neededDetails)
        attachments = yield self.getAttachments(build, key)
        
        postData = {}
        if key == "new":
            postData["text"] = "Buildbot started build <{}|{}>".format(build["url"], build["builder"]["name"])
        if key == "finished":
            postData["text"] = "Buildbot finished build {}".format(build["builder"]["name"])
            postData["attachments"] = attachments

        if self.channel:
            postData["channel"] = self.channel

        postData["icon_emoji"] = STATUS_EMOJIS.get(
            statusToString(build["results"]), ":facepalm:"
        )
        extra_params = yield self.getExtraParams(build, key)
        postData.update(extra_params)
        return postData

    # returns a Deferred that returns None
    def buildStarted(self, key, build):
        if self.reportBuildStated:
            return self.send(build, key[2])
        return None

    # returns a Deferred that returns None
    @defer.inlineCallbacks
    def buildFinished(self, key, build):
        yield utils.getDetailsForBuild(self.master, build)

        doSend = False
        if self.reportOnlyFailures:
            if isFailure(build["results"]):
                doSend = True

            elif self.reportFixedBuild:
                status = self.getPrevBuildResult(build)
                if isSuccess(build["results"]) and isFailure(status):
                    doSend = True
                        
        else:
            doSend = True

        if self.reportFixedBuild:
            self.storePrevBuildResult(build)

        if doSend:
            return self.send(build, key[2])
        return None

    def getExtraParams(self, build, event_name):
        return {}

    @defer.inlineCallbacks
    def send(self, build, key):
        postData = yield self.getBuildDetailsAndSendMessage(build, key)
        if not postData:
            return

        sourcestamps = build["buildset"]["sourcestamps"]

        for sourcestamp in sourcestamps:
            sha = sourcestamp["revision"]
            if sha is None:
                logger.info("no special revision for this")

            logger.info("posting to {url}", url=self.endpoint)
            try:
                if self.baseUrl:
                    # deprecated
                    response = yield self._http.post(self.endpoint, json=postData)
                else:
                    response = yield self._http.post("", json=postData)
                if response.code != 200:
                    content = yield response.content()
                    logger.error(
                        "{code}: unable to upload status: {content}",
                        code=response.code,
                        content=content,
                    )
            except Exception as e:
                logger.error(
                    "Failed to send status for {repo} at {sha}: {error}",
                    repo=sourcestamp["repository"],
                    sha=sha,
                    error=e,
                )

    def storePrevBuildResult(self, build):
        self.prevBuildResults[build["builder"]["name"]] = build["results"]

    def getPrevBuildResult(self, build):
        if build["builder"]["name"] in self.prevBuildResults:
            return self.prevBuildResults[build["builder"]["name"]]
        else:
            return None
