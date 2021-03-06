$(function () {

    var getImageSuccess = function (data) {
        $("#imageID").attr("src", "/currentimage");
        console.log("currentimage called");
    };

    var getImageSuccess2 = function (data) {
        $("#outsemsegID").attr("src", "/outsemseg");
        console.log("getImageSuccess2 called");
    };
    var getImageSuccess3 = function (data) {
        $("#outdepthID").attr("src", "/outdepth");
        console.log("getImageSuccess3 called");
    };

    var getImageFailure = function (data) {
        console.log("no image data.  sorry!!!");
    };

    var successResult = function (data) {

        removeLoading();
        console.log("removeLoading called!");

        var req = {
            url: "/currentimage",
            method: "get"
        };
        var promise = $.ajax(req);
        promise.then(getImageSuccess, getImageFailure);
        console.log("successResult called");

        var req2 = {
            url: "/outsemseg",
            method: "get"
        };
        var promise2 = $.ajax(req2);
        promise2.then(getImageSuccess2, getImageFailure);

        var req3 = {
            url: "/outdepth",
            method: "get"
        };
        var promise3 = $.ajax(req3);
        promise3.then(getImageSuccess3, getImageFailure);

    };
    var failureResult = function (data) {
        alert("that didn't work  so good");
    };

    var fileChange = function (evt) {
        dispLoading("computing...");
        // dispLoading();

        var fileOb = $("#fileField")[0].files[0];
        var formData = new FormData();
        formData.append("picfile", fileOb);
        var req = {
            url: "/predict",
            method: "post",
            processData: false,
            contentType: false,
            data: formData
        };

        var promise = $.ajax(req);
        promise.then(successResult, failureResult);
    };

    $("#fileField").change(fileChange);
});
